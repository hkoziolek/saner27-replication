# Architecture drift report  (commit orchardcore-rename_target-01)

## New code facts not represented in model
- new target `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.WebAnonMut.csproj`
- new edge `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.WebAnonMut.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Application.Cms.Targets/OrchardCore.Application.Cms.Targets.csproj`
- new edge `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.WebAnonMut.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Logging.NLog/OrchardCore.Logging.NLog.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Application.Cms.Targets/OrchardCore.Application.Cms.Targets.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Logging.NLog/OrchardCore.Logging.NLog.csproj`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` → `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.WebAnonMut.csproj` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Admin/OrchardCore.Admin.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.Pdf/OrchardCore.Media.Indexing.Pdf.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Search/OrchardCore.Search.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.AdminMenu.Abstractions/OrchardCore.AdminMenu.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Shortcodes.Abstractions/OrchardCore.Shortcodes.Abstractions.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.Markdown.Abstractions/OrchardCore.Markdown.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Data.YesSql/OrchardCore.Data.YesSql.csproj`: **OK**
