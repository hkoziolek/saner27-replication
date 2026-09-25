using System.Runtime.InteropServices;

namespace Calc;

/// Managed wrapper over the native NativeMath library (reached via P/Invoke).
public static class NativeMath
{
    [DllImport("NativeMath", EntryPoint = "native_add")]
    public static extern int Add(int a, int b);
}
