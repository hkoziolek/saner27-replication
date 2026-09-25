# Architecture drift report  (commit nopcommerce-merge_targets-01)

## New code facts not represented in model
- new edge `csharp:csproj:src/Libraries/Nop.Data/Nop.Data.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- new edge `csharp:csproj:src/Libraries/Nop.Services/Nop.Services.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- new edge `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`
- removed edge `csharp:csproj:src/Libraries/Nop.Data/Nop.Data.csproj` → `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`
- removed edge `csharp:csproj:src/Libraries/Nop.Services/Nop.Services.csproj` → `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`
- removed edge `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj` → `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`
- removed edge `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj` → `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `Core` -/-> `Data Access`: **OK**
- ✅ `Core` -/-> `Services`: **OK**
- ✅ `Core` -/-> `Web Framework`: **OK**
- ✅ `Core` -/-> `Web Application`: **OK**
- ✅ `Data Access` -/-> `Services`: **OK**
- ✅ `Data Access` -/-> `Web Framework`: **OK**
- ❌ `Data Access` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Libraries/Nop.Data/Nop.Data.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ✅ `Services` -/-> `Web Framework`: **OK**
- ❌ `Services` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Libraries/Nop.Services/Nop.Services.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Web Framework` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Payment Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Payments.CheckMoneyOrder/Nop.Plugin.Payments.CheckMoneyOrder.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Payments.Manual/Nop.Plugin.Payments.Manual.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Payments.PayPalCommerce/Nop.Plugin.Payments.PayPalCommerce.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Shipping & Tax Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Shipping.FixedByWeightByTotal/Nop.Plugin.Shipping.FixedByWeightByTotal.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Tax.Avalara/Nop.Plugin.Tax.Avalara.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Tax.FixedOrByCountryStateZip/Nop.Plugin.Tax.FixedOrByCountryStateZip.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Widget Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.FacebookPixel/Nop.Plugin.Widgets.FacebookPixel.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.GoogleAnalytics/Nop.Plugin.Widgets.GoogleAnalytics.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.Jotform/Nop.Plugin.Widgets.Jotform.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.Swiper/Nop.Plugin.Widgets.Swiper.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Misc & Other Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Brevo/Nop.Plugin.Misc.Brevo.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Forums/Nop.Plugin.Misc.Forums.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.News/Nop.Plugin.Misc.News.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Omnisend/Nop.Plugin.Misc.Omnisend.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Polls/Nop.Plugin.Misc.Polls.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.RFQ/Nop.Plugin.Misc.RFQ.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Zettle/Nop.Plugin.Misc.Zettle.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ✅ `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj` -/-> `csharp:csproj:src/Plugins/Nop.Plugin.DiscountRules.CustomerRoles/Nop.Plugin.DiscountRules.CustomerRoles.csproj`: **OK**
- ✅ `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Dynamics365/Nop.Plugin.Misc.Dynamics365.csproj` -/-> `csharp:csproj:src/Plugins/Nop.Plugin.Misc.AzureBlob/Nop.Plugin.Misc.AzureBlob.csproj`: **OK**
- ✅ `csharp:csproj:src/Plugins/Nop.Plugin.Misc.WebApi.Frontend/Nop.Plugin.Misc.WebApi.Frontend.csproj` -/-> `csharp:csproj:src/Plugins/Nop.Plugin.DiscountRules.CustomerRoles/Nop.Plugin.DiscountRules.CustomerRoles.csproj`: **OK**
- ✅ `csharp:csproj:src/Plugins/Nop.Plugin.Shipping.UPS/Nop.Plugin.Shipping.UPS.csproj` -/-> `csharp:csproj:src/Libraries/Nop.Core/Nop.Core.csproj`: **OK**
