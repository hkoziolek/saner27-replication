# Architecture drift report  (commit eshop-rename_target-02)

## New code facts not represented in model
- new target `csharp:csproj:src/HybridApp/HybridAppAnonMut.csproj`
- new edge `csharp:csproj:src/HybridApp/HybridAppAnonMut.csproj` → `csharp:csproj:src/WebAppComponents/WebAppComponents.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/HybridApp/HybridApp.csproj`
- removed edge `csharp:csproj:src/HybridApp/HybridApp.csproj` → `csharp:csproj:src/WebAppComponents/WebAppComponents.csproj`

## Suspected renames / moves (§7.3 — pin via id_aliases)
- `csharp:csproj:src/HybridApp/HybridApp.csproj` → `csharp:csproj:src/HybridApp/HybridAppAnonMut.csproj` (suspected rename/move)

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ❌ `Web App (Blazor Storefront)` -/-> `Event Bus`: **VIOLATION** — `csharp:csproj:src/WebApp/WebApp.csproj`→`csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj`
- ✅ `Event Bus` -/-> `Web App (Blazor Storefront)`: **OK**
- ✅ `csharp:csproj:src/Basket.API/Basket.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/EventBusRabbitMQ/EventBusRabbitMQ.csproj` -/-> `csharp:csproj:src/Ordering.Domain/Ordering.Domain.csproj`: **OK**
- ✅ `csharp:csproj:src/Ordering.API/Ordering.API.csproj` -/-> `csharp:csproj:src/Catalog.API/Catalog.API.csproj`: **OK**
- ✅ `csharp:csproj:src/WebApp/WebApp.csproj` -/-> `csharp:csproj:src/Ordering.Infrastructure/Ordering.Infrastructure.csproj`: **OK**
