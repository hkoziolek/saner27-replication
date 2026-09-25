/*
 * Eval §6.1 — WCA (Maqbool & Babri, TSE'07) + LIMBO (Andritsos & Tzerpos) baseline driver.
 *
 * Both are STRUCTURAL agglomerative clustering: unlike ARC they need no source text / topics —
 * they cluster the dependency-derived feature vectors directly (WCA via the Unbiased Ellenberg
 * measure, LIMBO via information loss). So this driver is the cheap counterpart to ArcRunner:
 * canonical graph in, clusters RSF out, using ARCADE's own Clusterer.run(WCA|LIMBO, …) — no
 * re-implementation (the §6.4 fairness control), no MALLET, and fully DETERMINISTIC (the
 * agglomerative tie-break is the TreeMap cluster order), unlike ARC.
 *
 * Usage:
 *   java -cp "ARCADE_Core.jar;arc" StructuralRunner <graph.rsf> <wca|limbo> <out.rsf> <k> <proj>
 *
 * k = preselected agglomerative stopping point (eval §6.4: = the reference cluster count, the
 * standard SAR protocol). All graph entities are clustered (full coverage) — we admit clusters
 * via ARCADE's "java"-profile path with an empty prefix filter, which applies no source-extension
 * filter, so WCA/LIMBO cover the whole graph like ACDC/dir/cc (and unlike ARC's C-sourced subset).
 */
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

import edu.usc.softarch.arcade.clustering.Architecture;
import edu.usc.softarch.arcade.clustering.Clusterer;
import edu.usc.softarch.arcade.clustering.ClusteringAlgorithmType;
import edu.usc.softarch.arcade.clustering.FeatureVectors;
import edu.usc.softarch.arcade.clustering.criteria.SerializationCriterion;
import edu.usc.softarch.arcade.clustering.criteria.StoppingCriterion;
import edu.usc.softarch.arcade.clustering.simmeasures.SimMeasure;
import edu.usc.softarch.arcade.facts.DependencyGraph;

public class StructuralRunner {

	public static void main(String[] args) throws Exception {
		if (args.length < 5) {
			System.err.println("usage: StructuralRunner <graph.rsf> <wca|limbo> <out.rsf> <k> <proj>");
			System.exit(2);
		}
		Path graphRsf = Paths.get(args[0]);
		String algoName = args[1].toLowerCase();
		Path outRsf = Paths.get(args[2]);
		int numClusters = Integer.parseInt(args[3]);
		String projName = args[4];

		ClusteringAlgorithmType algo;
		SimMeasure.SimMeasureType sim;
		if ("wca".equals(algoName)) {
			algo = ClusteringAlgorithmType.WCA;
			sim = SimMeasure.SimMeasureType.UEMNM;   // WCA-UENM: the canonical WCA similarity
		} else if ("limbo".equals(algoName)) {
			algo = ClusteringAlgorithmType.LIMBO;
			sim = SimMeasure.SimMeasureType.IL;      // LIMBO: information loss
		} else {
			System.err.println("unknown algorithm '" + algoName + "' (wca|limbo)");
			System.exit(2);
			return;
		}

		FeatureVectors fv = new FeatureVectors(DependencyGraph.readRsf(graphRsf.toString()));
		System.out.println("[" + algoName + "] " + fv.getFeatureVectorNames().size()
				+ " entities from " + graphRsf.getFileName());

		// 7-arg ctor: no DocTopics. language="java" + empty prefix ("") admits ALL entities
		// (the java admission path applies no source-extension filter) -> full graph coverage.
		Architecture arch = new Architecture(projName, "0", ".", sim, fv, "java", "");
		System.out.println("[" + algoName + "] " + arch.size() + " singleton clusters");

		StoppingCriterion stop = StoppingCriterion.makeStoppingCriterion(
				StoppingCriterion.Criterion.PRESELECTED, (double) numClusters, arch);
		SerializationCriterion ser = SerializationCriterion.makeSerializationCriterion(
				SerializationCriterion.Criterion.STEPCOUNT, Double.MAX_VALUE, arch);

		Architecture result = Clusterer.run(algo, arch, ser, stop, sim);
		System.out.println("[" + algoName + "] clustered to " + result.size()
				+ " clusters (target " + numClusters + ")");

		Files.createDirectories(outRsf.getParent());
		result.writeToRsf(outRsf.toString());
		System.out.println("[" + algoName + "] wrote " + outRsf);
	}
}
