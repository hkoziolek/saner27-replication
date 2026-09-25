// Consumes the managed Bridge assembly via a C++/CLI #using reference. This names the
// managed bridge boundary (native:clr:Bridge), which interop_resolve binds to the /clr
// Bridge.vcxproj target (whose output assembly is "Bridge").
#using <Bridge.dll>

using namespace System;

int main()
{
    auto calc = gcnew Bridge::Calculator();
    return calc->Add(2, 3);
}
