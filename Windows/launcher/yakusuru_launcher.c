/*
 * Yakusuru.exe — Windows launcher (the counterpart of Yakusuru.app on macOS).
 *
 *  - App already set up  → starts Yakusuru directly, no console window.
 *  - First run / broken / requirements changed → opens a console running Yakusuru.bat, which
 *    finds (or offers to install) Python and shows the one-time installation progress.
 *
 * The real logic lives in app\bootstrap.py and Yakusuru.bat; this program only decides which
 * of the two to start, so it never needs updating when the app changes.
 * Files dropped onto the icon (or passed on the command line) are forwarded to the app.
 *
 * Build (from any OS, see build.sh):  zig cc -target x86_64-windows-gnu -mwindows ...
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <bcrypt.h>
#include <shellapi.h>
#include <wchar.h>

#define APP L"Yakusuru"
#define BIG 4096

static wchar_t root[BIG], appdir[BIG], data[BIG];

static int exists(const wchar_t *p) { return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES; }

static void join(wchar_t *out, const wchar_t *a, const wchar_t *b) {
    _snwprintf(out, BIG, L"%s\\%s", a, b);
    out[BIG - 1] = 0;
}

static void fail(const wchar_t *msg) {
    MessageBoxW(NULL, msg, APP, MB_OK | MB_ICONERROR);
}

/* Folder layout: <root>\Windows\Yakusuru.exe and <root>\app\bootstrap.py.
   Also accept the exe being placed directly in <root>. */
static int find_root(void) {
    wchar_t exe[BIG], probe[BIG];
    if (!GetModuleFileNameW(NULL, exe, BIG)) return 0;
    wchar_t *s = wcsrchr(exe, L'\\');
    if (!s) return 0;
    *s = 0;                                         /* exe = folder of the exe */
    _snwprintf(probe, BIG, L"%s\\..\\app\\bootstrap.py", exe);
    if (exists(probe)) {
        _snwprintf(probe, BIG, L"%s\\..", exe);
        if (!GetFullPathNameW(probe, BIG, root, NULL)) return 0;
    } else {
        _snwprintf(probe, BIG, L"%s\\app\\bootstrap.py", exe);
        if (!exists(probe)) return 0;
        wcsncpy(root, exe, BIG);
    }
    join(appdir, root, L"app");
    return 1;
}

static int find_data(void) {
    wchar_t base[BIG], legacy[BIG];
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", base, BIG);
    if (!n || n >= BIG) return 0;
    join(data, base, L"Yakusuru");
    join(legacy, base, L"LanguageInterpreter");      /* the app's earlier name: adopt its env */
    if (!exists(data) && exists(legacy)) MoveFileW(legacy, data);
    return 1;
}

/* First 16 hex chars of SHA-256(file) — must match bootstrap.req_hash(). */
static int sha16(const wchar_t *path, char out[17]) {
    HANDLE f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE h = NULL;
    UCHAR digest[32], buf[65536];
    DWORD got;
    int ok = 0;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) == 0 &&
        BCryptCreateHash(alg, &h, NULL, 0, NULL, 0, 0) == 0) {
        ok = 1;
        while (ReadFile(f, buf, sizeof buf, &got, NULL) && got)
            if (BCryptHashData(h, buf, got, 0) != 0) { ok = 0; break; }
        if (ok && BCryptFinishHash(h, digest, 32, 0) == 0) {
            static const char hx[] = "0123456789abcdef";
            for (int i = 0; i < 8; i++) { out[2 * i] = hx[digest[i] >> 4]; out[2 * i + 1] = hx[digest[i] & 15]; }
            out[16] = 0;
        } else ok = 0;
    }
    if (h) BCryptDestroyHash(h);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(f);
    return ok;
}

/* Is the private environment installed and up to date with requirements\core.txt? */
static int env_ready(wchar_t *pythonw) {
    wchar_t marker[BIG], req[BIG];
    _snwprintf(pythonw, BIG, L"%s\\venv\\Scripts\\pythonw.exe", data);
    _snwprintf(marker, BIG, L"%s\\venv\\.core-installed", data);
    _snwprintf(req, BIG, L"%s\\requirements\\core.txt", appdir);
    if (!exists(pythonw) || !exists(marker)) return 0;
    char want[17], have[64] = {0};
    if (!sha16(req, want)) return 0;
    HANDLE f = CreateFileW(marker, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    DWORD got = 0;
    ReadFile(f, have, sizeof have - 1, &got, NULL);
    CloseHandle(f);
    return got >= 16 && memcmp(have, want, 16) == 0;   /* marker = "<reqhash>|<python tag>" */
}

/* Everything after argv[0] in our own command line, to forward unchanged. */
static const wchar_t *forwarded_args(void) {
    const wchar_t *c = GetCommandLineW();
    if (*c == L'"') { c++; while (*c && *c != L'"') c++; if (*c) c++; }
    else while (*c && *c != L' ' && *c != L'\t') c++;
    while (*c == L' ' || *c == L'\t') c++;
    return c;
}

static int run(wchar_t *cmdline, const wchar_t *cwd, DWORD flags, int wait, DWORD *code) {
    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(NULL, cmdline, NULL, NULL, FALSE, flags, NULL, cwd, &si, &pi)) return 0;
    if (wait) {
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, code);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

/* Console path: Yakusuru.bat shows install progress and can install Python with winget. */
static int run_installer(const wchar_t *args) {
    static wchar_t cmd[BIG * 2], bat[BIG], comspec[BIG];
    join(bat, root, L"Windows\\Yakusuru.bat");
    if (!exists(bat)) {
        fail(L"Windows\\Yakusuru.bat is missing from the Yakusuru folder. Download the app again.");
        return 1;
    }
    if (!GetEnvironmentVariableW(L"ComSpec", comspec, BIG)) wcscpy(comspec, L"cmd.exe");
    /* cmd /c ""path\Yakusuru.bat" args"  — the outer quotes are required by cmd's parsing rules */
    _snwprintf(cmd, BIG * 2, L"\"%s\" /c \"\"%s\" %s\"", comspec, bat, args);
    if (!run(cmd, appdir, CREATE_NEW_CONSOLE, 0, NULL)) {
        fail(L"Couldn't open a Command Prompt window to set up Yakusuru.");
        return 1;
    }
    return 0;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmd_unused, int show) {
    (void)inst; (void)prev; (void)cmd_unused; (void)show;
    if (!find_root()) {
        fail(L"Yakusuru.exe has to stay inside the Yakusuru folder (next to the \"app\" folder).\n\n"
             L"If you want it on the Desktop or Start menu, run \"Create Desktop Shortcut.bat\" "
             L"instead of moving the exe.");
        return 1;
    }
    const wchar_t *args = forwarded_args();
    wchar_t pythonw[BIG];
    if (!find_data() || !env_ready(pythonw) || wcsstr(args, L"--reset") || wcsstr(args, L"--setup-console"))
        return run_installer(args);

    /* Fast path: bootstrap.py under the environment's windowless Python. It double-checks the
       environment, starts the GUI detached and exits within a second or two. */
    static wchar_t cmd[BIG * 2];
    _snwprintf(cmd, BIG * 2, L"\"%s\" \"%s\\bootstrap.py\" %s", pythonw, appdir, args);
    DWORD code = 1;
    if (run(cmd, appdir, CREATE_NO_WINDOW, 1, &code) && code == 0) return 0;
    /* Something's off (Python moved, packages broken…): show the console path with its messages. */
    return run_installer(args);
}
