// Minimal native COM in-process server implementation stub. Its coclass identity (CLSID and
// ProgID) is declared in CalcServer.idl / CalcServer.rgs and captured as com_provides.
#include <windows.h>

STDAPI DllGetClassObject(REFCLSID, REFIID, void** ppv)
{
    *ppv = nullptr;
    return CLASS_E_CLASSNOTAVAILABLE;
}
