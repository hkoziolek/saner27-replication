# Architecture drift report  (commit eshop-remove_target-01)

## New code facts not represented in model
- _none_

## Model elements no longer found in code
- `csharp:csproj:src/Basket.API/Basket.API.csproj`
- removed edge `csharp:csproj:src/Basket.API/Basket.API.csproj` → `csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj`
- removed edge `csharp:csproj:src/Basket.API/Basket.API.csproj` → `csharp:csproj:src/eShop.ServiceDefaults/eShop.ServiceDefaults.csproj`
- removed edge `csharp:csproj:src/eShop.AppHost/eShop.AppHost.csproj` → `csharp:csproj:src/Basket.API/Basket.API.csproj`

## Coverage gaps (NOT drift — producing layer not extracted in both runs, §11.1)
- edge `csharp:csproj:src/Basket.API/Basket.API.csproj` → `contract:proto:basket` (coverage-gap)

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ❌ `Web App (Blazor Storefront)` -/-> `Event Bus`: **VIOLATION** — `csharp:csproj:src/WebApp/WebApp.csproj`→`csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj`
- ✅ `Event Bus` -/-> `Web App (Blazor Storefront)`: **OK**
- ✅ `csharp:csproj:src/Basket.API/Basket.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj` -/-> `csharp:csproj:src/Ordering.Domain/Ordering.Domain.csproj`: **OK**
- ✅ `csharp:csproj:src/Ordering.API/Ordering.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/WebApp/WebApp.csproj` -/-> `csharp:csproj:src/Ordering.Infrastructure/Ordering.Infrastructure.csproj`: **OK**
