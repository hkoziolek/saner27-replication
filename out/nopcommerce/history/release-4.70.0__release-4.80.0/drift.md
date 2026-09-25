# Architecture drift report  (commit release-4.70.0..release-4.80.0)

## New code facts not represented in model
- new target `csharp:csproj:src/Plugins/Nop.Plugin.Misc.OmnibusDirective/Nop.Plugin.Misc.OmnibusDirective.csproj`
- new target `csharp:csproj:src/Plugins/Nop.Plugin.Misc.PowerBI/Nop.Plugin.Misc.PowerBI.csproj`
- new target `csharp:csproj:src/Plugins/Nop.Plugin.Payments.AmazonPay/Nop.Plugin.Payments.AmazonPay.csproj`
- new target `csharp:csproj:src/Plugins/Nop.Plugin.Search.Lucene/Nop.Plugin.Search.Lucene.csproj`
- new target `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.Swiper/Nop.Plugin.Widgets.Swiper.csproj`
- new edge `csharp:csproj:src/Plugins/Nop.Plugin.Misc.OmnibusDirective/Nop.Plugin.Misc.OmnibusDirective.csproj` → `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj`
- new edge `csharp:csproj:src/Plugins/Nop.Plugin.Misc.PowerBI/Nop.Plugin.Misc.PowerBI.csproj` → `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj`
- new edge `csharp:csproj:src/Plugins/Nop.Plugin.Payments.AmazonPay/Nop.Plugin.Payments.AmazonPay.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- new edge `csharp:csproj:src/Plugins/Nop.Plugin.Search.Lucene/Nop.Plugin.Search.Lucene.csproj` → `csharp:csproj:src/Presentation/Nop.Web.Framework/Nop.Web.Framework.csproj`
- new edge `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.Swiper/Nop.Plugin.Widgets.Swiper.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`

## Model elements no longer found in code
- `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.NivoSlider/Nop.Plugin.Widgets.NivoSlider.csproj`
- removed edge `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.NivoSlider/Nop.Plugin.Widgets.NivoSlider.csproj` → `csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`

## Suspicious dependencies (declared but unused / layering violations)
- _none_

## Layering / fitness checks (§11.2)
- ✅ `Core` -/-> `Data Access`: **OK**
- ✅ `Core` -/-> `Services`: **OK**
- ✅ `Core` -/-> `Web Framework`: **OK**
- ✅ `Core` -/-> `Web Application`: **OK**
- ✅ `Data Access` -/-> `Services`: **OK**
- ✅ `Data Access` -/-> `Web Framework`: **OK**
- ✅ `Data Access` -/-> `Web Application`: **OK**
- ✅ `Services` -/-> `Web Framework`: **OK**
- ✅ `Services` -/-> `Web Application`: **OK**
- ✅ `Web Framework` -/-> `Web Application`: **OK**
- ❌ `Payment Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Payments.AmazonPay/Nop.Plugin.Payments.AmazonPay.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Payments.CheckMoneyOrder/Nop.Plugin.Payments.CheckMoneyOrder.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Payments.Manual/Nop.Plugin.Payments.Manual.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Payments.PayPalCommerce/Nop.Plugin.Payments.PayPalCommerce.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Shipping & Tax Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Tax.Avalara/Nop.Plugin.Tax.Avalara.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Widget Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.FacebookPixel/Nop.Plugin.Widgets.FacebookPixel.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.GoogleAnalytics/Nop.Plugin.Widgets.GoogleAnalytics.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.Swiper/Nop.Plugin.Widgets.Swiper.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Widgets.What3words/Nop.Plugin.Widgets.What3words.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
- ❌ `Misc & Other Plugins` -/-> `Web Application`: **VIOLATION** — `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Brevo/Nop.Plugin.Misc.Brevo.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Omnisend/Nop.Plugin.Misc.Omnisend.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`, `csharp:csproj:src/Plugins/Nop.Plugin.Misc.Zettle/Nop.Plugin.Misc.Zettle.csproj`→`csharp:csproj:src/Presentation/Nop.Web/Nop.Web.csproj`
