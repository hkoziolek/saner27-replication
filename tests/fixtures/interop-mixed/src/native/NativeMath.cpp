// The native shared library reached by the C# side via [DllImport("NativeMath")].
extern "C" __declspec(dllexport) int native_add(int a, int b)
{
    return a + b;
}
