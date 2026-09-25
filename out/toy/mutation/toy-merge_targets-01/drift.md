# Architecture drift report  (commit toy-merge_targets-01)

## New code facts not represented in model
- new edge `csharp:csproj:src/Messaging/Toy.Messaging.csproj` → `csharp:csproj:src/Domain/Toy.Domain.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/Common/Toy.Common.csproj`
- removed edge `csharp:csproj:src/Domain/Toy.Domain.csproj` → `csharp:csproj:src/Common/Toy.Common.csproj`
- removed edge `csharp:csproj:src/Messaging/Toy.Messaging.csproj` → `csharp:csproj:src/Common/Toy.Common.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/Common/Toy.Common.csproj` -/-> `csharp:csproj:src/Web/Toy.Web.csproj`: **OK**
- ❌ `csharp:csproj:src/Messaging/Toy.Messaging.csproj` -/-> `csharp:csproj:src/Domain/Toy.Domain.csproj`: **VIOLATION** — `csharp:csproj:src/Messaging/Toy.Messaging.csproj`→`csharp:csproj:src/Domain/Toy.Domain.csproj`
