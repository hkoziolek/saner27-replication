# Architecture drift report  (commit toy-add_forbidden_dep-01)

## New code facts not represented in model
- new edge `csharp:csproj:src/Common/Toy.Common.csproj` → `csharp:csproj:src/Web/Toy.Web.csproj`

## Model elements no longer found in code
- _none_

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ❌ `csharp:csproj:src/Common/Toy.Common.csproj` -/-> `csharp:csproj:src/Web/Toy.Web.csproj`: **VIOLATION** — `csharp:csproj:src/Common/Toy.Common.csproj`→`csharp:csproj:src/Web/Toy.Web.csproj`
- ✅ `csharp:csproj:src/Messaging/Toy.Messaging.csproj` -/-> `csharp:csproj:src/Domain/Toy.Domain.csproj`: **OK**
