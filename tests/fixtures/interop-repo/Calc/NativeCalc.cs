using System.Runtime.InteropServices;

namespace Calc;

/// <summary>
/// P/Invoke bindings to the native nativecalc shared library (classic DllImport style).
/// </summary>
internal static class NativeCalc
{
    [DllImport("nativecalc", EntryPoint = "nc_add")]
    internal static extern int Add(int a, int b);

    [DllImport("nativecalc")]
    internal static extern int Multiply(int a, int b);
}

/// <summary>
/// Source-generated interop binding to the crypto native library (LibraryImport style).
/// The library name includes a .dll extension to exercise extension-stripping.
/// </summary>
internal static partial class CryptoInterop
{
    [LibraryImport("crypto.dll")]
    internal static partial int HashData(nint buffer, int length);
}
