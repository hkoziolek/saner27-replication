# Architecture drift report  (commit orchardcore-merge_targets-04)

## New code facts not represented in model
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement.Liquid/OrchardCore.DisplayManagement.Liquid.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement/OrchardCore.DisplayManagement.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.Elasticsearch.Core/OrchardCore.Elasticsearch.Core.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.FileStorage.AzureBlob/OrchardCore.FileStorage.AzureBlob.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.Workflows.Abstractions/OrchardCore.Workflows.Abstractions.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Alias/OrchardCore.Alias.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Autoroute/OrchardCore.Autoroute.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.ContentFields/OrchardCore.ContentFields.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.ContentLocalization/OrchardCore.ContentLocalization.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Contents/OrchardCore.Contents.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.DataProtection.Azure/OrchardCore.DataProtection.Azure.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Facebook/OrchardCore.Facebook.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Flows/OrchardCore.Flows.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Html/OrchardCore.Html.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Lucene/OrchardCore.Lucene.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Markdown/OrchardCore.Markdown.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media/OrchardCore.Media.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Queries/OrchardCore.Queries.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Settings/OrchardCore.Settings.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Shortcodes/OrchardCore.Shortcodes.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Sitemaps/OrchardCore.Sitemaps.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Spatial/OrchardCore.Spatial.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Taxonomies/OrchardCore.Taxonomies.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Title/OrchardCore.Title.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Users/OrchardCore.Users.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Workflows/OrchardCore.Workflows.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement.Liquid/OrchardCore.DisplayManagement.Liquid.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement/OrchardCore.DisplayManagement.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Elasticsearch.Core/OrchardCore.Elasticsearch.Core.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.FileStorage.AzureBlob/OrchardCore.FileStorage.AzureBlob.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Abstractions/OrchardCore.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Workflows.Abstractions/OrchardCore.Workflows.Abstractions.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Liquid.Abstractions/OrchardCore.Liquid.Abstractions.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Admin/OrchardCore.Admin.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.Pdf/OrchardCore.Media.Indexing.Pdf.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Search/OrchardCore.Search.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.AdminMenu.Abstractions/OrchardCore.AdminMenu.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Shortcodes.Abstractions/OrchardCore.Shortcodes.Abstractions.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.Markdown.Abstractions/OrchardCore.Markdown.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Data.YesSql/OrchardCore.Data.YesSql.csproj`: **OK**
