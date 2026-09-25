/*
 * Eval §6.1 — ARC (Garcia et al., ASE'11) baseline driver over the canonical RSF export.
 *
 * ARC is concern/topic-based: it models each source entity as a distribution over LDA
 * topics and clusters by topic-distribution similarity. ARCADE_Core bundles both the ARC
 * clustering machinery (edu.usc.softarch.arcade.clustering.Clusterer with the ARCUEM/ARCUEMNM
 * similarity measures) AND the MALLET LDA implementation (cc.mallet.*) it depends on, but
 * exposes no single end-to-end CLI for it — the shipped MalletRunner shells out to an external
 * `mallet` binary with per-language stopword artifacts that the Core jar does not contain.
 *
 * This driver wires the pieces together IN PROCESS, using ARCADE's own implementations end to
 * end (no re-implementation — the §6.4 fairness control):
 *   1. read the canonical graph-<g>.rsf via ARCADE's DependencyGraph.readRsf -> FeatureVectors
 *      (so ARC consumes the SAME dependency graph as every other technique);
 *   2. run MALLET LDA (cc.mallet.topics.ParallelTopicModel) over the source text of each graph
 *      entity, each MALLET document NAMED EXACTLY by its RSF entity id so ARCADE's DocTopics
 *      name-matching aligns topic vectors to clusters with no fuzzy fallback;
 *   3. serialize the MALLET InstanceList (`vectors`) + TopicInferencer (`topicmodel`) into a
 *      work dir and let ARCADE's Architecture(... ARC ...) ctor load them via
 *      DocTopics.initializeSingleton (its native path);
 *   4. agglomeratively cluster with ARCADE's Clusterer.run(ARC, ...) to a preselected cluster
 *      count, and write the result as a `contain`-tuple RSF that run_arcade.py scores with the
 *      same mojo.MoJo / SystemEvo (a2a) implementations as ACDC.
 *
 * Usage:
 *   java -cp "ARCADE_Core.jar;arc" ArcRunner \
 *        <graph.rsf> <srcRoot> <file|target> <out.rsf> <c|java> <numTopics> <numClusters> <proj>
 *
 * Notes / honest limitations (mirrored in run_arcade.py and the eval notes):
 *   - ARCADE's DocTopicItem.isCSourced() recognises .c/.cpp/.h/.hpp/.ia/.icc/.p/.s/.tbl but
 *     NOT .hxx or .cc, so on systems whose entities use those extensions (ITK .hxx, abseil .cc)
 *     a subset of entities carry no topic vector and ARC drops them; the projection in
 *     run_arcade.py keeps scoring honest by reporting the surviving entity counts.
 *   - C# has no MALLET language profile in ARCADE (only c/java/python); we run it under the
 *     `java` profile (closest tokenizer + stopwords) over concatenated per-project .cs text.
 */
import java.io.File;
import java.io.FileOutputStream;
import java.io.ObjectOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import cc.mallet.pipe.CharSequenceLowercase;
import cc.mallet.pipe.CharSequence2TokenSequence;
import cc.mallet.pipe.Pipe;
import cc.mallet.pipe.SerialPipes;
import cc.mallet.pipe.TokenSequence2FeatureSequence;
import cc.mallet.pipe.TokenSequenceRemoveStopwords;
import cc.mallet.topics.ParallelTopicModel;
import cc.mallet.topics.TopicInferencer;
import cc.mallet.types.Instance;
import cc.mallet.types.InstanceList;

import edu.usc.softarch.arcade.clustering.Architecture;
import edu.usc.softarch.arcade.clustering.Clusterer;
import edu.usc.softarch.arcade.clustering.ClusteringAlgorithmType;
import edu.usc.softarch.arcade.clustering.FeatureVectors;
import edu.usc.softarch.arcade.clustering.criteria.SerializationCriterion;
import edu.usc.softarch.arcade.clustering.criteria.StoppingCriterion;
import edu.usc.softarch.arcade.clustering.simmeasures.SimMeasure;
import edu.usc.softarch.arcade.facts.DependencyGraph;

public class ArcRunner {

	public static void main(String[] args) throws Exception {
		if (args.length < 8) {
			System.err.println("usage: ArcRunner <graph.rsf> <srcRoot> <file|target> <out.rsf> "
					+ "<c|java> <numTopics> <numClusters> <projName>");
			System.exit(2);
		}
		Path graphRsf = Paths.get(args[0]);
		Path srcRoot = Paths.get(args[1]);
		String granularity = args[2];
		Path outRsf = Paths.get(args[3]);
		String language = args[4].toLowerCase();
		int numTopics = Integer.parseInt(args[5]);
		int numClusters = Integer.parseInt(args[6]);
		String projName = args[7];

		// 1. canonical graph -> ARCADE FeatureVectors (same input as every other technique)
		FeatureVectors fv = new FeatureVectors(DependencyGraph.readRsf(graphRsf.toString()));
		// stable corpus order (determinism): the MALLET fit depends on instance order, so sort
		// the entity list rather than relying on FeatureVectors' internal iteration order.
		List<String> entities = new ArrayList<>(fv.getFeatureVectorNames());
		java.util.Collections.sort(entities);
		System.out.println("[arc] " + entities.size() + " entities from " + graphRsf.getFileName());

		// 2. MALLET LDA over each entity's source text, doc name == RSF entity id
		Pipe pipe = buildPipe();
		InstanceList instances = new InstanceList(pipe);
		int withText = 0;
		for (String entity : entities) {
			String text = readSource(srcRoot, granularity, entity);
			if (text == null || text.isBlank()) continue;
			// DocTopics keys on Instance.getSource() and matches it to the entity cluster name.
			// Both matchers require source.contains("/" + proj + "-" + ver + "/"); then the
			//   C matcher (lang=c)    : source.endsWith("/" + entity)               [dots kept]
			//   Java matcher (lang=java): source.endsWith(entity.replace('.','/'))  [dots->slashes]
			// Reproduce the matching path shape per language so topic vectors align with no
			// fuzzy fallback (the bare entity id alone matches neither guard).
			String tail = "java".equals(language) ? entity.replace(".", "/") : entity;
			String source = "/" + projName + "-0/" + tail;
			instances.addThruPipe(new Instance(text, null, entity, source));
			withText++;
		}
		System.out.println("[arc] " + withText + "/" + entities.size()
				+ " entities had readable source text");
		if (withText == 0) { System.err.println("[arc] no source text resolved — aborting"); System.exit(3); }

		int iterations = Integer.getInteger("arc.iterations", 1000);
		int topics = Math.max(2, Math.min(numTopics, Math.max(2, withText / 2)));
		ParallelTopicModel model = new ParallelTopicModel(topics, 1.0 * topics, 0.01);
		model.addInstances(instances);
		// Fixed seed + single thread + sorted corpus pin everything WE control. NOTE this does
		// NOT make ARC reproducible: ARCADE's own DocTopics ctor derives each entity's topic
		// vector via TopicInferencer.getSampledDistribution(...), an UNSEEDED Gibbs sample, so
		// the recovered decomposition varies run-to-run (measured: eShop MoJoFM 20-33 over 5
		// runs). That variance is itself the eval-§9/RQ5 contrast with the pipeline's byte-
		// identical output — reported, not hidden. (Seeding it would mean editing ARCADE.)
		model.setRandomSeed(Integer.getInteger("arc.seed", 1));
		model.setNumThreads(1);
		model.setNumIterations(iterations);
		// MALLET writes a progress chatter to stdout; keep our stdout parseable, send it to stderr
		PrintStream realOut = System.out;
		System.setOut(System.err);
		model.estimate();
		System.setOut(realOut);
		System.out.println("[arc] LDA done: " + topics + " topics, 1000 iterations");

		// 3. serialize the MALLET artifacts ARCADE's DocTopics.initializeSingleton expects
		Path work = Files.createTempDirectory("arc-" + projName + "-");
		instances.save(work.resolve("vectors").toFile());
		TopicInferencer inferencer = model.getInferencer();
		try (ObjectOutputStream oos = new ObjectOutputStream(
				new FileOutputStream(work.resolve("topicmodel").toFile()))) {
			oos.writeObject(inferencer);
		}

		// 4. ARCADE's own ARC clustering. Initialize the DocTopics singleton from the MALLET
		//    artifacts (initializeSingleton(dir, name, ver) loads dir/vectors + dir/topicmodel),
		//    then let the Architecture(... ARC ...) ctor match topic vectors to entity clusters.
		edu.usc.softarch.arcade.topics.DocTopics.initializeSingleton(work.toString(), projName, "0");
		edu.usc.softarch.arcade.topics.DocTopics dt =
				edu.usc.softarch.arcade.topics.DocTopics.getSingleton(projName, "0");
		java.util.Collection<edu.usc.softarch.arcade.topics.DocTopicItem> items = dt.getCopy();
		System.out.println("[arc] DocTopics parsed " + items.size() + " items, numTopics="
				+ dt.getNumTopics());
		int csourced = 0; int shown = 0;
		for (edu.usc.softarch.arcade.topics.DocTopicItem it : items) {
			if (it.isCSourced()) csourced++;
			if (shown++ < 5) System.out.println("    src='" + it.getSource() + "' cSourced="
					+ it.isCSourced());
		}
		System.out.println("[arc] " + csourced + "/" + items.size() + " DocTopicItems are C-sourced");

		SimMeasure.SimMeasureType sim = SimMeasure.SimMeasureType.ARCUEM;
		// 8th ctor arg is the cluster-name prefix filter used by addClusterConditionally for the
		// java path (it calls .isEmpty() — must be "" not null); the c path ignores it.
		Architecture arch = new Architecture(
				projName, "0", srcRoot.toString(), sim, fv, language,
				work.toString(), "", false);
		System.out.println("[arc] architecture initialized: " + arch.size() + " singleton clusters"
				+ " (entities matched to a topic vector)");

		StoppingCriterion stop = StoppingCriterion.makeStoppingCriterion(
				StoppingCriterion.Criterion.PRESELECTED, (double) numClusters, arch);
		// never serialize intermediate steps — large step count so shouldSerialize() stays false
		SerializationCriterion ser = SerializationCriterion.makeSerializationCriterion(
				SerializationCriterion.Criterion.STEPCOUNT, Double.MAX_VALUE, arch);

		Architecture result = Clusterer.run(ClusteringAlgorithmType.ARC, arch, ser, stop, sim);
		System.out.println("[arc] clustered to " + result.size() + " clusters (target "
				+ numClusters + ")");

		Files.createDirectories(outRsf.getParent());
		result.writeToRsf(outRsf.toString());
		System.out.println("[arc] wrote " + outRsf);
	}

	private static Pipe buildPipe() {
		List<Pipe> pipes = new ArrayList<>();
		pipes.add(new CharSequenceLowercase());
		// identifiers + words; split on non-letters so camelCase stays whole (ARCADE-style)
		pipes.add(new CharSequence2TokenSequence(Pattern.compile("\\p{L}[\\p{L}\\p{N}_]+")));
		pipes.add(new TokenSequenceRemoveStopwords(false, false));
		pipes.add(new TokenSequence2FeatureSequence());
		return new SerialPipes(pipes);
	}

	/** Resolve an entity's source text. File granularity: the file itself. Target granularity
	 *  (C# csproj id): concatenated *.cs under the project directory. */
	private static String readSource(Path srcRoot, String granularity, String entity) {
		try {
			if ("file".equals(granularity)) {
				Path f = srcRoot.resolve(entity);
				if (!Files.isRegularFile(f)) return null;
				return new String(Files.readAllBytes(f), StandardCharsets.UTF_8);
			}
			// target granularity — only C# csproj ids are resolvable to a directory of sources
			String rel = entity.startsWith("csharp:csproj:") ? entity.substring("csharp:csproj:".length())
					   : entity.startsWith("csharp:") ? entity.substring(entity.lastIndexOf(':') + 1)
					   : entity;
			Path proj = srcRoot.resolve(rel);
			Path dir = Files.isDirectory(proj) ? proj : proj.getParent();
			if (dir == null || !Files.isDirectory(dir)) return null;
			try (Stream<Path> walk = Files.walk(dir)) {
				List<Path> cs = walk.filter(Files::isRegularFile)
						.filter(p -> p.toString().endsWith(".cs"))
						.filter(p -> !p.toString().contains("obj" + File.separator)
								&& !p.toString().contains("bin" + File.separator))
						.sorted()   // determinism: Files.walk order is filesystem-dependent
						.collect(Collectors.toList());
				if (cs.isEmpty()) return null;
				StringBuilder sb = new StringBuilder();
				for (Path p : cs) {
					sb.append(new String(Files.readAllBytes(p), StandardCharsets.UTF_8)).append('\n');
				}
				return sb.toString();
			}
		} catch (Exception e) {
			return null;
		}
	}
}
