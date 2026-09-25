# Architecture drift report  (commit orchardcore-merge_targets-03)

## New code facts not represented in model
- new edge `csharp:csproj:src/OrchardCore/OrchardCore.Application.Cms.Targets/OrchardCore.Application.Cms.Targets.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Module.Targets/OrchardCore.Module.Targets.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Flows/OrchardCore.Flows.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Menu/OrchardCore.Menu.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Notifications/OrchardCore.Notifications.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Themes/OrchardCore.Themes.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Users/OrchardCore.Users.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Admin.Abstractions/OrchardCore.Admin.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.ContentManagement/OrchardCore.ContentManagement.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.DisplayManagement/OrchardCore.DisplayManagement.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.ResourceManagement/OrchardCore.ResourceManagement.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Theme.Targets/OrchardCore.Theme.Targets.csproj`
- removed edge `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj` → `csharp:csproj:src/OrchardCore/OrchardCore.Users.Abstractions/OrchardCore.Users.Abstractions.csproj`
- removed edge `csharp:csproj:src/OrchardCore/OrchardCore.Application.Cms.Targets/OrchardCore.Application.Cms.Targets.csproj` → `csharp:csproj:src/OrchardCore.Themes/TheTheme/TheTheme.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/OrchardCore.Cms.Web/OrchardCore.Cms.Web.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Admin/OrchardCore.Admin.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Media.Indexing.Pdf/OrchardCore.Media.Indexing.Pdf.csproj` -/-> `csharp:csproj:src/OrchardCore.Modules/OrchardCore.Search/OrchardCore.Search.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.AdminMenu.Abstractions/OrchardCore.AdminMenu.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Shortcodes.Abstractions/OrchardCore.Shortcodes.Abstractions.csproj`: **OK**
- ✅ `csharp:csproj:src/OrchardCore/OrchardCore.Markdown.Abstractions/OrchardCore.Markdown.Abstractions.csproj` -/-> `csharp:csproj:src/OrchardCore/OrchardCore.Data.YesSql/OrchardCore.Data.YesSql.csproj`: **OK**
