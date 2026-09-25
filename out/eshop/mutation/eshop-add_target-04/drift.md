# Architecture drift report  (commit eshop-add_target-04)

## New code facts not represented in model
- new target `csharp:csproj:src/AnonMutant4/AnonMutant4.csproj`

## Model elements no longer found in code
- _none_

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ❌ `Web App (Blazor Storefront)` -/-> `Event Bus`: **VIOLATION** — `csharp:csproj:src/WebApp/WebApp.csproj`→`csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj`
- ✅ `Event Bus` -/-> `Web App (Blazor Storefront)`: **OK**
- ✅ `csharp:csproj:src/Basket.API/Basket.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj` -/-> `csharp:csproj:src/Ordering.Domain/Ordering.Domain.csproj`: **OK**
- ✅ `csharp:csproj:src/Ordering.API/Ordering.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/WebApp/WebApp.csproj` -/-> `csharp:csproj:src/Ordering.Infrastructure/Ordering.Infrastructure.csproj`: **OK**
