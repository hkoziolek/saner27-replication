// A managed C++/CLI bridge (/clr). It exposes the native NativeMath functions to managed
// callers, so it is a genuine managed<->native seam (tagged interop:cppcli by the extractor).
using namespace System;

namespace Bridge
{
    public ref class Calculator
    {
    public:
        int Add(int a, int b)
        {
            // gcnew marks this translation unit as managed C++/CLI.
            String^ label = gcnew String("add");
            (void)label;
            return a + b;
        }
    };
}
