# Architecture drift report  (commit toy-add_target-01)

## New code facts not represented in model
- new target `csharp:csproj:src/AnonMutant1/AnonMutant1.csproj`

## Model elements no longer found in code
- _none_

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `csharp:csproj:src/Common/Toy.Common.csproj` -/-> `csharp:csproj:src/Web/Toy.Web.csproj`: **OK**
- ✅ `csharp:csproj:src/Messaging/Toy.Messaging.csproj` -/-> `csharp:csproj:src/Domain/Toy.Domain.csproj`: **OK**
