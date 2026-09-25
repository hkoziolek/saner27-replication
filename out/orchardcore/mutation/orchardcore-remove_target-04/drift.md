# Architecture drift report  (commit orchardcore-remove_target-04)

## New code facts not represented in model
- _none_

## Model elements no longer found in code
- `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Contents/OrchardCore.Contents.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.DataLocalization/OrchardCore.DataLocalization.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Deployment/OrchardCore.Deployment.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Lucene/OrchardCore.Lucene.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Placements/OrchardCore.Placements.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Queries/OrchardCore.Queries.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Templates/OrchardCore.Templates.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Tenants/OrchardCore.Tenants.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Users/OrchardCore.Users.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Workflows/OrchardCore.Workflows.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement/OrchardCore.DisplayManagement.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Abstractions/OrchardCore.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Infrastructure.Abstractions/OrchardCore.Infrastructure.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Core/OrchardCore.Localization.Core.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore/OrchardCore.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Localization.Abstractions/OrchardCore.Localization.Abstractions.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Admin/OrchardCore.Admin.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.Pdf/OrchardCore.Media.Indexing.Pdf.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Search/OrchardCore.Search.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.AdminMenu.Abstractions/OrchardCore.AdminMenu.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Shortcodes.Abstractions/OrchardCore.Shortcodes.Abstractions.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.Markdown.Abstractions/OrchardCore.Markdown.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Data.YesSql/OrchardCore.Data.YesSql.csproj`: **OK**
