using System;
using System.Runtime.InteropServices;

namespace App;

// (a) Classic P/Invoke into the native NativeMath shared library. Resolves to the
//     cpp NativeMath target by normalized name (native:lib:NativeMath -> cpp target).
internal static class NativeMath
{
    [DllImport("NativeMath", EntryPoint = "native_add")]
    internal static extern int Add(int a, int b);

    // Precision check: a commented-out P/Invoke must NOT mint a phantom seam.
    // [DllImport("PhantomLib")] internal static extern int Ghost(int x);

    // Precision check: a string literal that merely *mentions* DllImport must not either.
    internal const string Doc = "we used to [DllImport(\"OldLib\")] this";
}

// (c) A [ComImport] coclass identity. Its [Guid] is the CLSID the native COM server
//     (CalcServer.vcxproj / CalcServer.idl) registers, so it resolves by identity.
[ComImport]
[Guid("3f2504e0-4f89-41d3-9a0c-0305e82c3301")]
internal class CalcServer
{
}

internal static class ComDriver
{
    internal static object Make()
    {
        var t = Type.GetTypeFromProgID("CalcServer.Object");
        return Activator.CreateInstance(t);
    }
}
