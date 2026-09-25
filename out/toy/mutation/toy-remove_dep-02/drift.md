# Architecture drift report  (commit toy-remove_dep-02)

## New code facts not represented in model
- _none_

## Model elements no longer found in code
- removed edge `csharp:csproj:src/Web/Toy.Web.csproj` → `csharp:csproj:src/Domain/Toy.Domain.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/Common/Toy.Common.csproj` -/-> `csharp:csproj:src/Web/Toy.Web.csproj`: **OK**
- ✅ `csharp:csproj:src/Messaging/Toy.Messaging.csproj` -/-> `csharp:csproj:src/Domain/Toy.Domain.csproj`: **OK**
