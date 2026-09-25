# Architecture drift report  (commit orchardcore-remove_target-02)

## New code facts not represented in model
- _none_

## Model elements no longer found in code
- `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Indexing.Abstractions/OrchardCore.Indexing.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Media.Abstractions/OrchardCore.Media.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.ResourceManagement/OrchardCore.ResourceManagement.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Application.Cms.Core.Targets/OrchardCore.Application.Cms.Core.Targets.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.OpenXML/OrchardCore.Media.Indexing.OpenXML.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Admin/OrchardCore.Admin.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.Pdf/OrchardCore.Media.Indexing.Pdf.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Search/OrchardCore.Search.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.AdminMenu.Abstractions/OrchardCore.AdminMenu.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Shortcodes.Abstractions/OrchardCore.Shortcodes.Abstractions.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.Markdown.Abstractions/OrchardCore.Markdown.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Data.YesSql/OrchardCore.Data.YesSql.csproj`: **OK**
