#!/usr/bin/env python3
"""EU4 引擎内补丁工具(纯 Python，不需要 mod 脚本；装完补丁就退出，不常驻)。

原理
----
在 eu4.exe 进程里给几处引擎代码挂上小段机器码，先判断是否涉及玩家国家，不涉及就照常执行原代码，
所以 AI 之间的战斗、围城不受影响。游戏每次启动都要重装(Steam 启动项会自动装)。

功能(只对玩家国家)
----
1. 全歼：玩家一方打赢陆战，败方部队直接全军覆没(走原版全歼流程，原版的兵力返还等规则不变)。
   海战不处理：原版的全歼只针对陆军。
2. 骰子：
   - 陆战、海战每次掷骰：玩家一方固定最大(骰子面数 - 1，默认 9)，敌方固定 0。
   - 强攻要塞每次掷骰：同上。
   - 围城每次掷骰：玩家攻城固定 14(最大)；敌人围攻玩家控制的要塞固定 1(最小，原版规则下 1 可能被算成 2)。
   - 指挥官、地形、渡河等加减值照原版计算。
3. 无损耗：玩家的陆军部队损耗为 0(含行军、围城、海运时的损耗)。
4. 宣战无惩罚：玩家宣战时稳定度等惩罚为 0(无理由宣战、撕毁休战协议、联姻、良好关系等各项)，
   宣战也不增加侵略扩张。
5. 盟友必定参战：
   - 玩家宣战或被宣战时，所有盟友像附庸一样直接加入战争，不需要对方同意、不消耗人情；
     宣战窗口里盟友和附庸一样显示为“强制参战”(不能取消)，并计入兵力对比。已被选作敌方共同交战国的除外。
   - 战争中用“召集参战”召集盟友仍照原版(要对方同意、消耗人情)。
6. 不触发恶性随机事件：随机触发的事件里，所有选项都以坏处为主的(稳定度/威望/钱/点数下降、叛军、
   负面修正等)，发给玩家时直接不触发；AI 照常。清单见下面的 BAD_EVENTS(脚本静态分析得出，约 700 个)。
7. 成就(对所有人生效)：装了 mod、改过的游戏/存档、非铁人模式也能获得成就。
   - 脚本触发器 ironman 恒按“是铁人”判断(成就条件里都写了 ironman = yes)，
     所以其他脚本里的 ironman 判断也会受影响。
   - 难度、AI 难度、幸运国家、开局设置、多人游戏、指定国家等其他成就条件不变。

兼容性
------
- 适用版本：1.37.5.0(EU4 已停止更新)。
- 补丁位置用特征码在 eu4.exe 里查找；某处找不到或不唯一，只跳过那一项并写日志。

用法
----
    Steam 启动项：  pythonw.exe 本脚本 %command%     (启动游戏并自动安装；之后游戏自己重启也会重装，
                                                     游戏退出 1 分钟内没再启动才结束)
    手动安装：      pythonw.exe 本脚本 --install      (游戏已在运行时)
    撤销补丁：      pythonw.exe 本脚本 --remove
    查看特征码结果：python 本脚本 --status
日志：%TEMP%\\eu4_buff.log
"""
import argparse
import ctypes
import os
import re
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ.get("TEMP", str(HERE))) / "eu4_buff.log"
EXE = "eu4.exe"
_quiet = False


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if not _quiet:
        print(line, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 512 * 1024:
            LOG.write_text("", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 进程/内存辅助
# ---------------------------------------------------------------------------
class _PE32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]


class P:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")

    @staticmethod
    def find_pid(name):
        k = P.k32
        snap = k.CreateToolhelp32Snapshot(0x2, 0)
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(pe)
        pid = None
        ok = k.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.decode(errors="ignore").lower() == name and P.alive(pe.th32ProcessID):
                pid = pe.th32ProcessID
                break
            ok = k.Process32Next(snap, ctypes.byref(pe))
        k.CloseHandle(snap)
        return pid

    @staticmethod
    def alive(pid):
        """进程还在运行(已退出但还被别人持有句柄的不算)。"""
        P.k32.OpenProcess.restype = wintypes.HANDLE
        h = P.k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return True  # 打不开就当它在运行，后面 open_process 再报错
        code = wintypes.DWORD()
        P.k32.GetExitCodeProcess(h, ctypes.byref(code))
        P.k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE

    @staticmethod
    def open_process(pid):
        P.k32.OpenProcess.restype = wintypes.HANDLE
        h = P.k32.OpenProcess(0x1F0FFF, False, pid)
        if not h:
            raise OSError(f"OpenProcess 失败(错误 {ctypes.get_last_error()})")
        return h

    @staticmethod
    def wait_exit(h):
        """等进程(h)退出。"""
        P.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        P.k32.WaitForSingleObject(h, 0xFFFFFFFF)
        P.k32.CloseHandle(h)

    @staticmethod
    def single_instance(name):
        """建一个命名互斥量；已有同名的(另一个守候进程在运行)返回 None。返回的句柄要一直留着。"""
        P.k32.CreateMutexW.restype = wintypes.HANDLE
        P.k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = P.k32.CreateMutexW(None, False, name)
        if not h or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return h

    @staticmethod
    def module_base(h):
        P.psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        mods = (wintypes.HMODULE * 1)()
        need = wintypes.DWORD()
        P.psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(need), 3)
        return mods[0]

    @staticmethod
    def exe_path(h):
        P.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        P.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return Path(buf.value)

    @staticmethod
    def read_mem(h, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        P.k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
        return buf.raw[:got.value]

    @staticmethod
    def write_mem(h, addr, raw):
        done = ctypes.c_size_t()
        return bool(P.k32.WriteProcessMemory(h, ctypes.c_void_p(addr), raw, len(raw), ctypes.byref(done)))

    @staticmethod
    def alloc_near(h, target, size):
        """在 target 的 ±1.8GB 内分配可执行内存(jmp rel32 够得着)。"""
        P.k32.VirtualAllocEx.restype = ctypes.c_void_p
        P.k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                         wintypes.DWORD, wintypes.DWORD]
        a = max((target - 0x70000000) & ~0xFFFF, 0x10000)
        while a < target + 0x70000000:
            p = P.k32.VirtualAllocEx(h, ctypes.c_void_p(a), size, 0x3000, 0x40)
            if p:
                return p
            a += 0x10000
        return None

    @staticmethod
    def patch_code(h, addr, raw):
        """暂停全部线程再改指令，避免改到一半被执行。"""
        P.nt.NtSuspendProcess(h)
        try:
            return P.write_mem(h, addr, raw)
        finally:
            P.nt.NtResumeProcess(h)


P.k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
P.k32.WriteProcessMemory.argtypes = P.k32.ReadProcessMemory.argtypes


class Image:
    """从磁盘上的 eu4.exe 读取(始终是未打补丁的原始代码)。"""

    def __init__(self, path):
        d = self.data = Path(path).read_bytes()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        self.secs = {}
        for i in range(nsec):
            o = pe + 24 + optsz + 40 * i
            name = d[o:o + 8].rstrip(b"\0").decode()
            vsize, rva, rawsz, raw = struct.unpack_from("<IIII", d, o + 8)
            self.secs[name] = (rva, vsize, raw, rawsz)
        rva, vsize, raw, rawsz = self.secs[".text"]
        self.text_rva, self.text = rva, d[raw:raw + rawsz]

    def rva_to_off(self, rva):
        for _n, (r, v, raw, rawsz) in self.secs.items():
            if r <= rva < r + rawsz:
                return raw + rva - r
        return None

    def bytes_at(self, rva, n):
        o = self.rva_to_off(rva)
        return self.data[o:o + n] if o is not None else b""

    def find_sig(self, sig):
        """特征码(十六进制字节，'??' 通配)在代码段里的全部命中 RVA；" | " 分隔多个候选(不同游戏版本)。"""
        if " | " in sig:
            return sorted({h for alt in sig.split(" | ") for h in self.find_sig(alt)})
        toks = sig.split()
        pat = re.compile(b"".join(b"." if t == "??" else re.escape(bytes([int(t, 16)])) for t in toks), re.S)
        best, cur = (0, 0), None
        for i, t in enumerate(toks + ["??"]):
            if t != "??":
                cur = cur if cur is not None else i
            else:
                if cur is not None and i - cur > best[1] - best[0]:
                    best = (cur, i)
                cur = None
        s0, s1 = best
        seed = bytes(int(t, 16) for t in toks[s0:s1])
        hits, start = [], 0
        while True:
            p = self.text.find(seed, start)
            if p < 0:
                break
            b0 = p - s0
            if b0 >= 0 and pat.match(self.text, b0):
                hits.append(self.text_rva + b0)
            start = p + 1
        return hits


# ---------------------------------------------------------------------------
# 特征码与机器码(机器码全部在模拟器里测过)
# ---------------------------------------------------------------------------
SIGS = {
    # 取玩家国家标签(游戏对象+0x1E60/+0x1E68)；被内联在很多地方，用来解出游戏对象全局指针
    "PLAYER": (
        "48 8B 1D ?? ?? ?? ?? 80 BB 66 1E 00 00 07 75 ?? 80 BB 6F 1E 00 00 00 74 ?? 48 8B 9B 68 1E 00 00 "
        "EB ?? 48 8B 9B 60 1E 00 00"
    ),
    # 陆战掷骰函数(+0x2F 处 idiv 的操作数是战斗骰子面数)
    "DICEFN": (
        "40 53 48 83 EC 20 8B 05 ?? ?? ?? ?? 48 8B D9 83 F8 FF 74 ?? 89 41 18 48 83 C4 20 5B C3 BA 0D 02 "
        "00 00 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 99 F7 3D ?? ?? ?? ?? 89 53 18"
    ),
    # 战斗每日更新(陆战、海战共用)：新阶段开始时双方掷骰之后(r14 = 战斗)
    "LANDDICE1": "49 8B 4E 20 E8 ?? ?? ?? ?? 49 8B 4E 28 E8 ?? ?? ?? ?? 49 8B 06 49 8B CE",
    # 战斗每日更新(陆战、海战共用)：阶段内再次掷骰之后(r14 = 战斗)
    "LANDDICE2": "49 8B 4E 20 E8 ?? ?? ?? ?? 49 8B 4E 28 E8 ?? ?? ?? ?? 41 C7 46 38 00 00 00 00",
    # 强攻要塞每日更新：双方掷骰之后(r13 = 强攻)
    "ASSAULTDICE": (
        "BA ?? ?? 00 00 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 99 F7 3D ?? ?? ?? ?? 89 57 18 48 8B 05 ?? ?? "
        "?? ?? 8B 88 D0 1D 00 00 41 89 4D 4C"
    ),
    # 围城每阶段掷骰(1..14)之后(rbp = 围城, r8d = 点数)
    "SIEGEDICE": (
        "BA ?? ?? 00 00 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 44 8B C0 B8 93 24 49 92 41 F7 E8 41 03 D0 C1 "
        "FA 03 8B CA C1 E9 1F 03 D1 6B CA 0E 44 2B C1 41 FF C0 44 89 45 6C 45 8B D0"
    ),
    # 战斗结束结算(rcx 战斗, rdx 胜方, r8 败方)；+0x162 是调用原版全歼函数的 call
    "ENDBATTLE": (
        "48 89 5C 24 10 48 89 6C 24 18 48 89 74 24 20 57 41 54 41 55 41 56 41 57 48 83 EC 20 45 0F B6 E1 "
        "4D 8B F8 4C 8B EA 4C 8B F1 33 FF 4C 8B 92 E8 00 00 00 4D 85 D2 74 ?? 66 0F 1F 84 00 00 00 00 00 "
        "4D 8B 02 4D 8B 52 10 33 C9 4D 8B 48 30 49 8B C1 49 3B C1 72 ?? 49 3B 40 38 73 ?? 48 8B 10 48 83 "
        "C0 08 03 4A 28 EB ?? 03 F9 4D 85 D2 75 ?? 33 DB 49 8B 97 E8 00 00 00 48 85 D2 74 ?? 0F 1F 40 00 "
        "4C 8B 0A 48 8B 52 10 33 C9 4D 8B 51 30 49 8B C2 49 3B C2 72 ?? 49 3B 41 38 73 ?? 4C 8B 00 48 83 "
        "C0 08 41 03 48 28 EB ?? 03 D9 48 85 D2 75 ?? 69 0D ?? ?? ?? ?? E8 03 00 00 BE FF FF FF FF 85 C9 "
        "75 ?? 8B EE EB ?? 8D 87 73 93 18 00 3D 0D 58 39 00 77 ?? 69 C7 E8 03 00 00 99 F7 F9 8B E8 EB ?? "
        "48 63 C7 48 63 C9 48 69 C0 E8 03 00 00 48 99 48 F7 F9 48 8B E8 49 8B 16 49 8B CE FF 52 60 83 F8 "
        "01 75 ?? 49 8B 06 49 8B CE FF 50 70 84 C0 75 ?? 69 0D ?? ?? ?? ?? E8 03 00 00 85 C9 74 ?? 8D 87 "
        "73 93 18 00 3D 0D 58 39 00 77 ?? 69 C7 E8 03 00 00 99 F7 F9 8B F0 EB ?? 48 63 C7 48 63 C9 48 69 "
        "C0 E8 03 00 00 48 99 48 F7 F9 48 8B F0 3B F3 7F ?? 3B EB 7E ?? 45 0F B6 CC 4D 8B C7 49 8B D5 49 "
        "8B CE E8 ?? ?? ?? ??"
    ),
    # 部队损耗计算(rcx 部队, rdx 结果指针)
    "ATTRITION": (
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8D 6C 24 D9 48 81 EC "
        "A0 00 00 00 4D 8B E8 4C 8B E2 4C 8B F1 33 C9 89 4D 57 49 8B 86 18 02 00 00 48 85 C0 74 ?? 48 8B "
        "80 E8 00 00 00 4C 8B 40 28 41 0F B6 80 28 02 00 00 A8 08 75 ?? 84 C0 8B 1D ?? ?? ?? ?? 79 ?? 8B "
        "1D ?? ?? ?? ?? 48 89 4C 24 20 45 33 C9 41 B8 D7 01 00 00 48 8D 55 57 49 8B CE E8 ?? ?? ?? ?? 8B "
        "00 05 E8 03 00 00 48 63 C8 48 63 C3 48 0F AF C8"
    ),
    # 宣战惩罚计算(rcx 宣战对象，+0x10 宣战方；rdx 结果指针；第 5 参数选惩罚类型)
    "STABHIT": (
        "48 89 5C 24 20 4C 89 44 24 18 48 89 54 24 10 48 89 4C 24 08 55 56 57 41 54 41 55 41 56 41 57 48 "
        "8D AC 24 40 FA FF FF 48 81 EC C0 06 00 00 41 0F B6 D9 4D 8B E0 48 8B C2 48 8B F1 45 33 ED 44 38 "
        "69 40 74 ?? 44 89 2A E9 ?? ?? ?? ?? 41 8B FD 48 8B 49 10 48 8B 05 ?? ?? ?? ?? 48 C1 E9 20 48 0F "
        "BF C9 48 8B 80 18 01 00 00 48 8B 0C C8 48 81 C1 C0 28 00 00"
    ),
    # 执行宣战：算出宣战侵略扩张(edi)之后、加上之前(rbx = 宣战对象)
    "DOWAE": (
        "4C 8B 05 ?? ?? ?? ?? 49 81 C0 D0 1D 00 00 40 88 74 24 30 C6 44 24 28 01 40 88 74 24 20 41 B1 01 "
        "E8 ?? ?? ?? ?? 8B C7 C1 E8 1F 84 C0 75 ??"
    ),
    # 宣战召集循环的同盟分支(+0x30 挂钩，r12 关系条目，rdi 召集方)；+0x25 的 je 跳到“直接加入战争”
    "ALLYJOIN": (
        "4C 8B 56 08 49 8B 8A 88 00 00 00 4D 8B 82 80 00 00 00 4C 3B C1 74 ?? 48 C1 E8 20 49 8B 10 48 C1 "
        "EA 20 66 3B D0 74 ?? 49 83 C0 08 4C 3B C1 75 ?? 49 8B 54 24 0C 48 8B C2 48 C1 E8 38 84 C0 48 0F "
        "44 D7"
    ),
    # 生成参战方列表时把宣战方的盟友加入我方([rsp+0x60] 查询条件：类型 1、+4 宣战方、+0xC 强制参战标志)；+0x1D 挂钩
    "ALLYFLAG": (
        "48 8B 85 50 03 00 00 48 8B 40 10 C7 44 24 60 01 00 00 00 48 89 44 24 64 C6 44 24 6C 00 0F 28 44 "
        "24 60 66 0F 7F 45 70 45 33 C0 48 8B 44 24 30 48 8B 90 B8 19 00 00"
    ),
    # 触发事件(rdx 事件, r8 作用域)：事件 +0x14 数字 ID(命名空间基数 + 序号)，+0x7E0 标题键，+0x180 立即效果
    "EVFIRE": (
        "48 89 5C 24 18 48 89 4C 24 08 55 56 57 41 54 41 55 41 56 41 57 48 83 EC 30 4D 8B F8 48 8B F2 48 "
        "8B 2D ?? ?? ?? ?? 80 BA 5F 0A 00 00 00 74 ?? 48 8B CD E8 ?? ?? ?? ?? 84 C0 0F 85 ?? ?? ?? ??"
    ),
    # 成就：比较存档校验和，结果写到成就管理器 +1(存档校验通过)
    "ACHSAVE": "49 3B DC 75 ?? 4C 8B C3 E8 ?? ?? ?? ?? 85 C0 75 ?? B3 01 EB ?? 32 DB 41 88 5F 01",
    # 成就：设置成就管理器 +2(游戏未修改)
    "ACHMOD": (
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 48 8B 05 ?? ?? ?? ?? 0F B6 FA 48 8B F1 4C 8B 80 48 "
        "03 00 00 49 8B 58 30 48 85 DB 74 ?? 48 8B 03 48 8B CB FF 50 18 48 8B 03 48 8B CB FF 50 30 40 88 "
        "7E 02 48 8B 5C 24 30 48 8B 74 24 38 48 83 C4 20 5F C3 48 8B 5C 24 30 48 8B 74 24 38 40 88 79 02"
    ),
    # 成就：能否获得成就的判断里，铁人检查(游戏信息 +9)
    "ACHIRON1": (
        "40 38 7A 14 0F 84 ?? ?? ?? ?? 40 38 7E 02 0F 84 ?? ?? ?? ?? 45 84 C9 75 ?? 40 38 7A 09 0F 84 ?? "
        "?? ?? ??"
    ),
    # 成就：列出不能获得成就的原因时，“不是铁人”那一条
    "ACHIRON2": "45 32 FF 4D 85 F6 0F 84 ?? ?? ?? ?? 40 38 7A 09 0F 85 ?? ?? ?? ??",
    # 脚本触发器 ironman 的求值函数(比较游戏铁人标志 +0x24D0 和触发器的值 +0x68)
    "ACHTRIG": (
        "48 8B 05 ?? ?? ?? ?? 80 B8 DB 74 00 00 00 74 ?? B0 01 C3 0F B6 80 D0 24 00 00 38 41 68 0F 94 C0 "
        "C3"
    ),
}

CAVES = {
    "DICE14": (
        "000000000000000000000000000000009c5051524150415141524153e85b0000004983faff7444498b4e20e8ba"
        "0000004189c0498b4e28e8ae0000004139c0742a488b05c0ffffff8b10ffca790231d2498b4e204d8b4e284585"
        "c075034c87c989511841c7411800000000415b415a415941585a59589de9ee00000049c7c2ffffffff488b0576"
        "ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889"
        "d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20"
        "664439d2c380c901c35331c04885c974764889cb4c8b8b000100004c8b9b080100004d85c974244c89d84c29c8"
        "483d0020000077164d39d97311498b5110e8aeffffff743d4983c120ebea4c8b8be800000041bb000400004d85"
        "c9742141ffcb741c498b014885c0740e488b90d0000000e87bffffff740a4d8b4910ebda31c0eb05b801000000"
        "5bc3"
    ),
    "DICE13": (
        "000000000000000000000000000000009c5051524150415141524153e85b0000004983faff7444498b4d20e8ba"
        "0000004189c0498b4d28e8ae0000004139c0742a488b05c0ffffff8b10ffca790231d2498b4d204d8b4d284585"
        "c075034c87c989511841c7411800000000415b415a415941585a59589de9ee00000049c7c2ffffffff488b0576"
        "ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889"
        "d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20"
        "664439d2c380c901c35331c04885c974764889cb4c8b8b000100004c8b9b080100004d85c974244c89d84c29c8"
        "483d0020000077164d39d97311498b5110e8aeffffff743d4983c120ebea4c8b8be800000041bb000400004d85"
        "c9742141ffcb741c498b014885c0740e488b90d0000000e87bffffff740a4d8b4910ebda31c0eb05b801000000"
        "5bc3"
    ),
    "SIEGE": (
        "00000000000000009c505152415141524153e8470000004983faff7432488b4d20e8a600000085c0740841b80e"
        "000000eb1d488b45304885c07414488b9070040000e86d000000750641b801000000415b415a41595a59589de9"
        "ee00000049c7c2ffffffff488b0594ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f"
        "1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
        "4889d148c1e93884c9740948c1ea20664439d2c380c901c35331c04885c974764889cb4c8b8b000100004c8b9b"
        "080100004d85c974244c89d84c29c8483d0020000077164d39d97311498b5110e8aeffffff743d4983c120ebea"
        "4c8b8be800000041bb000400004d85c9742141ffcb741c498b014885c0740e488b90d0000000e87bffffff740a"
        "4d8b4910ebda31c0eb05b8010000005bc3"
    ),
    "WIPE": (
        "00000000000000000000000000000000505152415041514152415348833de5ffffff00745de8680000004983fa"
        "ff7452488b4c2420e8c600000085c07444488b4c2418e8b800000085c07536488b4c24284885c9742c534889e3"
        "4883e4f04883ec20488b01ff50604889dc5b83f8017511415b415a415941585a5958ff2586ffffff415b415a41"
        "5941585a5958e9ee00000049c7c2ffffffff488b0560ffffff488b004885c0743f488b90601e000080b8661e00"
        "0007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1"
        "ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c35331c04885c974764889cb4c8b8b"
        "000100004c8b9b080100004d85c974244c89d84c29c8483d0020000077164d39d97311498b5110e8aeffffff74"
        "3d4983c120ebea4c8b8be800000041bb000400004d85c9742141ffcb741c498b014885c0740e488b90d0000000"
        "e87bffffff740a4d8b4910ebda31c0eb05b8010000005bc3"
    ),
    "ATTR": (
        "00000000000000005152415250e8340000004983faff7427488b4c24184885c9741d488b91d0000000e86e0000"
        "00750f58415a5a59c702000000004889d0c358415a5a59eb6e49c7c2ffffffff488b05acffffff488b004885c0"
        "743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c97414"
        "4889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "NOPENALTY": (
        "00000000000000005152415250e82c0000004983faff741f488b4c2418488b5110e86e000000750f58415a5a59"
        "c702000000004889d0c358415a5a59eb6e49c7c2ffffffff488b05b4ffffff488b004885c0743f488b90601e00"
        "0080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080"
        "f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "NOAE": (
        "00000000000000009c5051524152e81b0000004983faff740d488b5310e862000000750231ff415a5a59589deb"
        "6e49c7c2ffffffff488b05c4ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000"
        "007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d1"
        "48c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "ALLYJOIN": (
        "000000000000000000000000000000009c5051524152e82c0000004983faff741e41837c24080175164889fae8"
        "6c000000750c415a5a59589dff25c9ffffff415a5a59589deb6e49c7c2ffffffff488b05abffffff488b004885"
        "c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974"
        "144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901"
        "c3"
    ),
    "ALLYFLAG": (
        "00000000000000005051524152e8240000004983faff7417488b942484000000e8670000007508c684248c0000"
        "0001415a5a5958eb6e49c7c2ffffffff488b05bcffffff488b004885c0743f488b90601e000080b8661e000007"
        "751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20"
        "440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "EVBLOCK": (
        "000000000000000000000000000000005051524150415141524153e8190100004983faff0f84020100004d89c1"
        "4d85c90f84f6000000498d412c4180794b007404498d4144488b104889d148c1e93884c97535496341284885c0"
        "0f88cc0000004869c0102e0000488b0d92ffffff488b094885c90f84b2000000480381a81c0000488b90680400"
        "00e8020100000f85990000004c8b4c24204d85c90f848b0000004981c1e00700004d8b59104d85db747b4981fb"
        "000100007772498379181072034d8b0948b825232284e49cf2cb48b9b301000000010000410fb6114831d0480f"
        "afc149ffc149ffcb75ed4c8b0d16ffffff4d85c974354d8b194531d24d39da732a4b8d0c1a48d1e9498b54c908"
        "4839c2740d72054989cbebe34c8d5101ebdd415b415a415941585a5958c3415b415a415941585a5958eb6e49c7"
        "c2ffffffff488b05b9feffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407"
        "488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e9"
        "3884c9740948c1ea20664439d2c380c901c3"
    ),
}

# (名称, 特征码键, 挂钩处相对命中处的偏移, 被搬走的原指令长度, 机器码键, 数据区各项)
# 数据区：glob = 游戏对象全局指针，dside = 战斗骰子面数(COMBAT_DICE_SIDE)，wipe = 原版全歼函数，
#         direct = 召集循环里“盟友直接加入战争”的分支，
#         @badev = 恶性事件标题的哈希表(安装时由 BAD_EVENTS 生成，写在补丁内存里)
HOOKS = (
    ("战斗骰子(新阶段)", "LANDDICE1", 0x12, 6, "DICE14", ("glob", "dside")),
    ("战斗骰子(阶段内)", "LANDDICE2", 0x12, 8, "DICE14", ("glob", "dside")),
    ("强攻骰子", "ASSAULTDICE", 0x22, 6, "DICE13", ("glob", "dside")),
    ("围城骰子", "SIEGEDICE", 0x32, 7, "SIEGE", ("glob",)),
    ("玩家打赢陆战即全歼", "ENDBATTLE", 0, 5, "WIPE", ("glob", "wipe")),
    ("宣战无稳定度等惩罚", "STABHIT", 0, 5, "NOPENALTY", ("glob",)),
    ("宣战不加侵略扩张", "DOWAE", 0x25, 5, "NOAE", ("glob",)),
    ("盟友像附庸一样直接参战", "ALLYJOIN", 0x30, 5, "ALLYJOIN", ("glob", "direct")),
    ("参战方列表里盟友算强制参战", "ALLYFLAG", 0x1D, 5, "ALLYFLAG", ("glob",)),
    ("玩家部队无损耗", "ATTRITION", 0, 5, "ATTR", ("glob",)),
    ("玩家不触发恶性随机事件", "EVFIRE", 0, 5, "EVBLOCK", ("glob", "@badev")),
)


# 直接改写(不需要机器码)：(名称, 特征码键, 改写处相对命中处的偏移, 原字节, 新字节)
DIRECT = (
    # 比较存档校验和：不一致时把“存档校验通过”设为否(xor bl,bl)，改成 mov bl,1
    ("成就:存档校验恒通过", "ACHSAVE", 0x15, "32db", "b301"),
    # 设置“游戏未修改”标志(mov [reg+2],dil)，改成恒存 1：装 mod 也算未修改
    ("成就:游戏未修改(有 mod)", "ACHMOD", 0x3E, "40887e02", "c6460201"),
    ("成就:游戏未修改(有 mod)2", "ACHMOD", 0x5C, "40887902", "c6410201"),
    # 能否获得成就：不是铁人就判否的 je，改成空操作
    ("成就:非铁人也可以", "ACHIRON1", 0x1D, "0f84dc000000", "909090909090"),
    # 列出不能获得成就的原因时，“不是铁人”那一条的 jne 改成必跳过
    ("成就:不提示非铁人", "ACHIRON2", 0x10, "0f85c2000000", "90e9c2000000"),
    # 脚本触发器 ironman 的求值：读游戏的铁人标志(movzx eax, byte [rax+0x24D0])改成 mov eax,1
    ("成就:ironman 触发器恒为是", "ACHTRIG", 0x13, "0fb680d0240000", "b8010000009090"),
)


# 恶性随机事件：(事件 ID, 标题键, 汉化标题)。按标题键识别(引擎里事件对象只存了标题键，数字 ID 要到运行时才定)。
# 由脚本分析生成：原版 + 已启用 mod 里随机触发(on_actions 的 random_events、各 pulse 的 events、平均触发时间)
# 的事件中，每个选项的负面效果都多于正面效果的；会清除标记、结束事变/灾难等改流程状态的事件和隐藏事件不算；
# 标题键和非恶性事件共用的也不算。想放行某个事件，删掉对应一行即可。
BAD_EVENTS = (
    ('1022', 'EVTNAME1022', '贸易控制'),
    ('anglican_events.15', 'anglican_events.15.t', '不遵奉国教的君主'),
    ('anglican_events.5', 'anglican_events.5.t', '马普雷拉特宣传册'),
    ('anglican_events.6', 'anglican_events.6.t', '祈祷书叛乱'),
    ('aow_events.1', 'aow_events.1.t', '的困境'),
    ('aow_events.11', 'aow_events.11.t', '瘟疫蔓延'),
    ('aow_events.15', 'aow_events.15.t', '留守的家庭'),
    ('aow_events.19', 'aow_events.19.t', '一个令人高度不安的指责'),
    ('aow_events.27', 'aow_events.27.t', '的信徒叛变了！'),
    ('aow_events.28', 'aow_events.28.t', '内外交困'),
    ('army_professionalism_events.12', 'army_professionalism_events.12.t', '缺少军官'),
    ('army_professionalism_events.4', 'army_professionalism_events.4.t', '延迟发薪'),
    ('army_professionalism_events.9', 'army_professionalism_events.9.t', '私人承包商'),
    ('buddhism_events.1', 'buddhism_events.1.t', '大臣辞职'),
    ('buddhism_events.17', 'buddhism_events.17.t', '施舍之德'),
    ('buddhism_events.18', 'buddhism_events.18.t', '苦行持戒之德'),
    ('buddhism_events.27', 'buddhism_events.27.t', '强盗首领归来'),
    ('buddhism_events.28', 'buddhism_events.28.t', '神话依然存在'),
    ('buddhism_events.3', 'buddhism_events.3.t', '僧人破戒'),
    ('buddhism_events.35', 'buddhism_events.35.t', '萨满教僧侣'),
    ('buddhism_events.4', 'buddhism_events.4.t', '僧院支持篡位者'),
    ('buddhism_events.5', 'buddhism_events.5.t', '的先例'),
    ('canal_events.11', 'canal_events.EVTNAME11', '运河公司阴谋'),
    ('canal_events.23', 'canal_events.EVTNAME23', '运河与河流交会'),
    ('central_african.2', 'central_african.2.t', '抄录《伊波尼亚》'),
    ('central_african.7', 'central_african.7.t', '姆布迪埃与发生冲突'),
    ('china_events.1', 'china_events.1.t', '通商条例'),
    ('china_events.19', 'china_events.19.t', '河运闹剧'),
    ('china_events.21', 'china_events.21.t', '金银外流'),
    ('china_events.22', 'china_events.22.t', '翻修宝塔'),
    ('china_events.23', 'china_events.23.t', '边疆豪杰'),
    ('china_events.34', 'china_events.34.t', '文房四宝'),
    ('china_events.38', 'china_events.38.t', '科举舞弊'),
    ('china_events.39', 'china_events.39.t', '回收多余纸币'),
    ('china_events.42', 'china_events.42.t', '朝堂之争'),
    ('china_events.46', 'china_events.46.t', '洪水泛滥'),
    ('china_events.47', 'china_events.47.t', '丝绸之路上的强盗'),
    ('china_events.48', 'china_events.48.t', '长廊旁的阴影'),
    ('china_events.49', 'china_events.49.t', '日食凶兆'),
    ('china_events.50', 'china_events.50.t', '不忠的官僚'),
    ('china_events.52', 'china_events.52.t', '瘟疫来袭'),
    ('china_events.53', 'china_events.53.t', '暗夜刀锋'),
    ('china_events.54', 'china_events.54.t', '嫔妃之子'),
    ('china_events.56', 'china_events.56.t', '悲观的见解'),
    ('china_events.58', 'china_events.58.t', '大学士对月呼号'),
    ('china_events.59', 'china_events.59.t', '运漕银的船沉了'),
    ('china_events.60', 'china_events.60.t', '假传圣旨'),
    ('china_events.8', 'china_events.8.t', '监守自盗的司库'),
    ('colonial.11', 'colonial.EVTNAME11', '防御是折中方案'),
    ('colonial.15', 'colonial.EVTNAME15', '我们的殖民地遭受了罗马热病的冲击！'),
    ('colonial.3', 'EVTNAME1421', '布道士煽动'),
    ('colonial.4', 'EVTNAME1422', ' 腐败的行政官'),
    ('conquest_of_paradise.300', 'colonial_nation.EVTNAME300', '禁止毛皮贸易'),
    ('conquest_of_paradise.301', 'colonial_nation.EVTNAME301', '年的收成'),
    ('colonial_nation.182', 'colonial_nation.182.t', '腐败的总督'),
    ('colonial_nation.184', 'colonial_nation.184.t', '债务缠身！'),
    ('colonial_nation.185', 'colonial_nation.185.t', '废除王室特许状'),
    ('colonial_nation.190', 'colonial_nation.190.t', '擅离职守的'),
    ('colonial_nation.75', 'colonial_nation.EVTNAME75', '传播革命'),
    ('confucian_events.14', 'confucian_events.14.t', '正道与礼制'),
    ('consort_events.315', 'consort_events.315.t', '王室私生子'),
    ('consort_events.53', 'consort_events.53.t', '国家事务'),
    ('coptic_flavor.31', 'coptic_flavor.31.t', '白色修道院'),
    ('corruption_event.11', 'corruption_event.11.t', '「失踪」'),
    ('corruption_event.12', 'corruption_event.12.t', '腐败的陆军将领'),
    ('corruption_event.13', 'corruption_event.13.t', '腐败的海军将领'),
    ('corruption_event.14', 'corruption_event.14.t', '外国商人不信任我们'),
    ('corruption_event.15', 'corruption_event.15.t', '政府机能崩溃'),
    ('corruption_event.16', 'corruption_event.16.t', '佣兵勒索当地民众'),
    ('corruption_event.17', 'corruption_event.17.t', '不称职的行政官员'),
    ('corruption_event.18', 'corruption_event.18.t', '过度扩张'),
    ('corruption_event.19', 'corruption_event.19.t', '与腐败关联'),
    ('corruption_event.20', 'corruption_event.20.t', '贿赂泛滥'),
    ('corruption_event.5', 'corruption_event.5.t', '清正廉洁'),
    ('corruption_event.6', 'corruption_event.6.t', '腐败的税吏'),
    ('corruption_event.8', 'corruption_event.8.t', '奸诈的走私犯'),
    ('corruption_event.9', 'corruption_event.9.t', '群体不信任我们'),
    ('cultural_event.1', 'cultural_event.1.t', '喜剧演出'),
    ('cultural_event.11', 'cultural_event.11.t', '南德意志风琴传统乐派'),
    ('cultural_event.12', 'cultural_event.12.t', '支持巴洛克式音乐'),
    ('cultural_event.13', 'cultural_event.13.t', '国家史诗'),
    ('cultural_event.2', 'cultural_event.2.t', '自由，平等，博爱'),
    ('cultural_event.4', 'cultural_event.4.t', '赞助佛罗伦萨画派'),
    ('cultural_event.5', 'cultural_event.5.t', '最后的骑士竞技大会'),
    ('cultural_event.6', 'cultural_event.6.t', '特许成立圣路加公会'),
    ('cultural_event.7', 'cultural_event.7.t', '荷兰式绘画的黄金时代'),
    ('3051', 'EVTNAME3051', '文化动荡'),
    ('culture_religion_events.17', 'culture_religion_events.17.t', '人日渐焦虑'),
    ('dotf.7', 'dotf.7.t', '教会堡垒'),
    ('devotion_events.117', 'devotion_events.117.t', '征用改革宗教堂'),
    ('devotion_events.119', 'devotion_events.119.t', '马头酒馆谣言'),
    ('devotion_events.15', 'devotion_events.15.t', '统治遭受挑战'),
    ('devotion_events.16', 'devotion_events.16.t', '禁止朝圣和迷信活动'),
    ('devotion_events.17', 'devotion_events.17.t', '与的宿怨'),
    ('devotion_events.18', 'devotion_events.18.t', '疯狂的先知'),
    ('devotion_events.2', 'devotion_events.2.t', '要塞维护'),
    ('devotion_events.207', 'devotion_events.207.t', '腐败的官员'),
    ('devotion_events.208', 'devotion_events.208.t', '宫廷里的卡菲勒'),
    ('devotion_events.209', 'devotion_events.209.t', '反对腐败的政变'),
    ('devotion_events.210', 'devotion_events.210.t', '正统反对者'),
    ('devotion_events.212', 'devotion_events.212.t', '长老会的失信'),
    ('devotion_events.213', 'devotion_events.213.t', '顾问被控以物配主'),
    ('devotion_events.218', 'devotion_events.218.t', '财产纠纷'),
    ('devotion_events.22', 'devotion_events.22.t', '衰落的统治'),
    ('devotion_events.23', 'devotion_events.23.t', '的腐败'),
    ('devotion_events.4', 'devotion_events.4.t', '过高的税收'),
    ('9008', 'EVTNAME9008', '与吉利支丹关系紧张'),
    ('9014', 'EVTNAME9014', '的踏绘'),
    ('9050', 'EVTNAME9050', '大米歉收'),
    ('dutch_republic.10', 'dutch_republic.10.t', '背弃共和'),
    ('dutch_republic.20', 'dutch_republic.20.t', '政变'),
    ('dutch_republic.21', 'dutch_republic.21.t', '意见分歧'),
    ('dutch_republic.24', 'dutch_republic.24.t', '议会派希望维持现状'),
    ('dutch_republic.30', 'dutch_republic.30.t', '的辩论'),
    ('9452', 'EVTNAME9452', '宫廷政变'),
    ('9459', 'EVTNAME9459', '打猎事故'),
    ('9460', 'EVTNAME9460', '家族秘密？'),
    ('9462', 'EVTNAME9462', '狡猾的官僚'),
    ('dynastic_events.2', 'dynastic_events.2.t', '新生女儿'),
    ('elective_monarchy.4', 'elective_monarchy.4.t', '密谋反对家族的'),
    ('1071', 'EVTNAME1071', '国力下滑'),
    ('1073', 'EVTNAME1073', '国家分裂'),
    ('1076', 'EVTNAME1076', '开拓殖民地'),
    ('espionage_events.8', 'espionage_events.8.t', '怀疑邻国'),
    ('exploration_events.12', 'exploration_events.12.t', '醉酒的探险家'),
    ('exploration_events.15', 'exploration_events.15.t', '死神来了'),
    ('exploration_events.6', 'exploration_events.6.t', '不受欢迎的乘客'),
    ('extended_regency_events.5', 'extended_regency_events.5.t', '寻宝'),
    ('fetishist_flavor.131', 'fetishist_flavor.131.t', '照料先逝之人'),
    ('fetishist_flavor.132', 'fetishist_flavor.132.t', '被质疑的统治者'),
    ('fetishist_flavor.152', 'fetishist_flavor.152.t', '姆瓦里的愤怒'),
    ('fetishist_flavor.153', 'fetishist_flavor.153.t', '的信仰'),
    ('fetishist_flavor.174', 'fetishist_flavor.174.t', '不好的预兆'),
    ('fetishist_flavor.208', 'fetishist_flavor.208.t', '恩凯之怒'),
    ('fetishist_flavor.310', 'fetishist_flavor.310.t', '死者之宫殿'),
    ('fetishist_flavor.408', 'fetishist_flavor.408.t', '繁衍仪式'),
    ('fetishist_flavor.420', 'fetishist_flavor.420.t', '亵渎安息日戒律'),
    ('fetishist_flavor.457', 'fetishist_flavor.457.t', '的宗教对峙'),
    ('fetishist_flavor.500', 'fetishist_flavor.500.t', '的会议'),
    ('fetishist_flavor.511', 'fetishist_flavor.511.t', '圣文'),
    ('flavor_bur.14', 'flavor_bur.EVTNAME14', '沙罗勒伯爵与《血腥敕令》'),
    ('flavor_bur.15', 'flavor_bur.EVTNAME15', '马孔伯爵与阿拉斯联盟'),
    ('flavor_eng.9104', 'flavor_eng.EVTNAME9104', '百年战争的终结'),
    ('flavor_eng.9113', 'flavor_eng.EVTNAME9113', '珍·格蕾'),
    ('flavor_eng.9118', 'flavor_eng.EVTNAME9118', '北方叛乱'),
    ('flavor_eng.9122', 'flavor_eng.EVTNAME9122', '休·奥尼尔的爱尔兰叛乱'),
    ('flavor_eng.9129', 'flavor_eng.EVTNAME9129', '宗教放任敕令和宗教审查法案'),
    ('flavor_eng.9139', 'flavor_eng.EVTNAME9139', '詹姆斯党叛乱'),
    ('flavor_eng.9143', 'flavor_eng.EVTNAME9143', '詹姆斯党叛乱'),
    ('flavor_eng.9250', 'flavor_eng.EVTNAME9250', '缺少继承人'),
    ('flavor_fra.3119', 'flavor_fra.EVTNAME3119', '贝亚恩的新教徒起义'),
    ('flavor_fra.3122', 'flavor_fra.EVTNAME3122', '投石党动乱'),
    ('flavor_fra.3128', 'flavor_fra.EVTNAME3128', '新教徒被逐出法兰西'),
    ('flavor_gbr.118', 'flavor_gbr.118.t', '第五王国派'),
    ('flavor_geo.7', 'flavor_geo.7.t', '德米特里乌斯的野心'),
    ('flavor_hab.13', 'flavor_hab.13.t', '维也纳瘟疫'),
    ('flavor_hab.3194', 'flavor_hab.EVTNAME3194', '布拉格抛出窗外事件'),
    ('flavor_hab.3195', 'flavor_hab.EVTNAME3195', '奥地利农民起义'),
    ('flavor_hab.3203', 'flavor_hab.EVTNAME3203', '萨尔茨堡大主教的清洗'),
    ('flavor_per.10', 'flavor_per.10.t', '穆沙沙教团的兴起'),
    ('flavor_per.11', 'flavor_per.11.t', '萨法维教团的兴起'),
    ('flavor_per.3560', 'flavor_per.EVTNAME3560', '奇兹尔巴什在安纳托利亚的暴动'),
    ('flavor_per.44', 'flavor_per.44.t', '祆教研究'),
    ('flavor_per.6', 'flavor_per.EVTNAME6', '阿富汗叛乱'),
    ('flavor_per.62', 'flavor_per.62.t', '祆教吸引贵族'),
    ('flavor_por.3273', 'flavor_por.EVTNAME3273', '流行性瘟疫'),
    ('flavor_por.3278', 'flavor_por.EVTNAME3278', '地震摧毁里斯本'),
    ('flavor_por.3280', 'flavor_por.EVTNAME3280', '塔沃拉家族阴谋'),
    ('flavor_rus.27', 'flavor_rus.EVTNAME3435', '普加乔夫起义'),
    ('flavor_rus.308', 'flavor_rus.308.t', '大起义！'),
    ('flavor_rus.350', 'flavor_rus.350.t', '农民的命运'),
    ('flavor_spa.3155', 'flavor_spa.EVTNAME3155', '平民社团起义'),
    ('flavor_spa.3158', 'flavor_spa.EVTNAME3158', '国家破产'),
    ('flavor_spa.3164', 'flavor_spa.EVTNAME3164', '国家破产'),
    ('flavor_spa.3165', 'flavor_spa.EVTNAME3165', '流行性瘟疫'),
    ('flavor_spa.3168', 'flavor_spa.EVTNAME3168', '葡萄牙民族起义'),
    ('flavor_spa.3169', 'flavor_spa.EVTNAME3169', '伊比利亚的叛乱'),
    ('flavor_spa.3170', 'flavor_spa.EVTNAME3170', '意大利人起义'),
    ('flavor_spa.4', 'flavor_spa.4.t', '统一西班牙'),
    ('flavor_swe.1628', 'flavor_swe.EVTNAME1628', '瓦萨号的沉没'),
    ('flavor_swe.1743', 'flavor_swe.EVTNAME1743', '达拉纳大叛乱'),
    ('flavor_swe.3240', 'flavor_swe.EVTNAME3240', '德·拉·加迪摄政'),
    ('flavor_tur.105', 'flavor_tur.EVTNAME105', '平民化'),
    ('flavor_tur.111', 'flavor_tur.EVTNAME111', '耶尼切里讨薪'),
    ('flavor_tur.265', 'flavor_tur.265.t', '确保继承安全'),
    ('flavor_tur.275', 'flavor_tur.275.t', '奇兹尔巴什起义'),
    ('flavor_tur.276', 'flavor_tur.276.t', '沙赫库卢起义'),
    ('flavor_tur.285', 'flavor_tur.285.t', '糟糕的经济政策'),
    ('flavor_tur.3368', 'flavor_tur.EVTNAME3368', '杰姆苏丹的叛乱'),
    ('flavor_tur.3395', 'flavor_tur.EVTNAME3395', '安纳托利亚瘟疫流行'),
    ('flavor_ven.13', 'flavor_ven.13.t', '香料贸易受到威胁'),
    ('flavor_ven.20', 'flavor_ven.20.t', '威尼斯的瘟疫'),
    ('flavor_ven.27', 'flavor_ven.27.t', '巴尔纳伯贵族'),
    ('flavor_ven.30', 'flavor_ven.30.t', '年大瘟疫'),
    ('free_cities.10', 'free_cities.10.t', '贵族起义'),
    ('free_cities.3', 'free_cities.3.t', '大街上的干草叉和火把'),
    ('free_cities.8', 'free_cities.8.t', '海关和税收优惠'),
    ('harem_events.22', 'harem_events.22.t', '和王子们'),
    ('harem_events.23', 'harem_events.23.t', '扮演国王的游戏'),
    ('harem_events.5', 'harem_events.5.t', '谋夺大位'),
    ('harem_events.7', 'harem_events.7.t', '恐惧丝绫赐死'),
    ('hindu_events.38', 'hindu_events.38.t', '象头神的诞辰'),
    ('hindu_events.44', 'hindu_events.44.t', '太阳神庙'),
    ('hindu_events.46', 'hindu_events.46.t', '季节变换'),
    ('hindu_events.50', 'hindu_events.50.t', '上师的冥想'),
    ('4043', 'EVTNAME4043', '贸易亏损'),
    ('4044', 'EVTNAME4043', '贸易亏损'),
    ('industrialization_events.10', 'industrialization_events.10.t', '纺织业的发展'),
    ('industrialization_events.11', 'industrialization_events.11.t', '造纸工业的发展'),
    ('industrialization_events.12', 'industrialization_events.12.t', '玻璃工业的发展'),
    ('industrialization_events.17', 'industrialization_events.17.t', '对新的生产技术的敌意'),
    ('industrialization_events.18', 'industrialization_events.18.t', '穷困的工匠'),
    ('industrialization_events.19', 'industrialization_events.19.t', '疾病在传播'),
    ('industrialization_events.20', 'industrialization_events.20.t', '发生矿难'),
    ('industrialization_events.21', 'industrialization_events.21.t', '失火'),
    ('innovativeness_events.11', 'innovativeness_events.11.t', '南方大陆'),
    ('innovativeness_events.2', 'innovativeness_events.2.t', '的发明'),
    ('innovativeness_events.6', 'innovativeness_events.6.t', '赞助'),
    ('inti_events.11', 'inti_events.11.t', '互惠'),
    ('764', 'EVTNAME764', '失去永久教宗使节的头衔'),
    ('mamluk_events.3', 'mamluk_events.3.t', '埃米尔的叛乱'),
    ('mamluk_events.5', 'mamluk_events.5.t', '宫廷政变'),
    ('mamluk_events.8', 'mamluk_events.8.t', '朝觐队伍受到袭击'),
    ('mch_missions.5', 'mch_missions.5.t', '国姓爷逃往台湾'),
    ('maya_religion_events.1', 'maya_religion_events.1.t', '滋养众神'),
    ('maya_religion_events.11', 'maya_religion_events.11.t', '梦魇'),
    ('maya_religion_events.12', 'maya_religion_events.12.t', '献祭传统'),
    ('maya_religion_events.4', 'maya_religion_events.4.t', '创造和毁灭的周期'),
    ('mercantilism_events.5', 'mercantilism_events.5.t', '，备受争议的地方保护主义者'),
    ('mercs.11', 'mercs.11.t', '拖欠的薪水'),
    ('mercs.3', 'mercs.3.t', '劫掠抵薪'),
    ('expel_minority_events.2', 'expel_minority_events.2.t', '团结'),
    ('expel_minority_events.3', 'expel_minority_events.3.t', '最后的人'),
    ('expel_minority_events.5', 'expel_minority_events.5.t', '罢免行政官员'),
    ('2060', 'EVTNAME2060', '传教士生病了'),
    ('2061', 'EVTNAME2061', '宗教抵抗'),
    ('2062', 'EVTNAME2062', '土著袭击'),
    ('2063', 'EVTNAME2063', '宗教动乱'),
    ('2064', 'EVTNAME2064', '恶毒的流言'),
    ('2065', 'EVTNAME2065', '宗教危机'),
    ('2067', 'EVTNAME2067', '传教活动'),
    ('monsoon_events.1', 'monsoon_events.1.t', '遭遇极端雨季'),
    ('monsoon_events.2', 'monsoon_events.2.t', '的雨季未如期而至'),
    ('monsoon_events.4', 'monsoon_events.4.t', '饥荒'),
    ('muslim_dlc.27', 'muslim_dlc.EVTNAME27', '什叶对逊尼'),
    ('muslim_dlc.28', 'muslim_dlc.EVTNAME28', '逊尼对什叶'),
    ('muslim_piety.20', 'muslim_piety.20.t', '一个新的'),
    ('muslim_school_events.10', 'muslim_school_events.10.t', '宗教学者的分歧'),
    ('muslim_school_events.11', 'muslim_school_events.11.t', '搬弄法庭'),
    ('muslim_school_events.13', 'muslim_school_events.13.t', '的抗议'),
    ('muslim_school_events.15', 'muslim_school_events.15.t', '城'),
    ('nahuatl_events.11', 'nahuatl_events.11.t', '流浪的巫师'),
    ('nahuatl_events.15', 'nahuatl_events.15.t', '乡间的巫师回来了'),
    ('nahuatl_events.16', 'nahuatl_events.16.t', '西瓦泰泰奥'),
    ('nahuatl_events.18', 'nahuatl_events.18.t', '橡胶足球球赛'),
    ('nahuatl_events.31', 'nahuatl_events.31.t', '地震'),
    ('nahuatl_events.6', 'nahuatl_events.6.t', '身膏烈日'),
    ('nahuatl_events.7', 'nahuatl_events.7.t', '汝非生此隅……'),
    ('native_americans.14', 'native_americans.14.t', '太阳舞蹈的准备'),
    ('native_americans.24', 'native_americans.24.t', '土丘建造者'),
    ('native_americans.3', 'native_americans.3.t', '我们在北方的新家'),
    ('native_americans.30', 'native_americans.30.t', '耻辱柱'),
    ('native_americans.33', 'native_americans.33.t', '失踪的动物'),
    ('native_americans.34', 'native_americans.34.t', '鹰扬'),
    ('native_americans.38', 'native_americans.38.t', '橡子稀缺'),
    ('native_americans.4', 'native_americans.4.t', '我们在沙漠的新家'),
    ('native_americans.40', 'native_americans.40.t', '社区汗屋'),
    ('native_federations_event.5', 'native_federations_event.5.t', '联盟面临的问题'),
    ('norse_events.7', 'norse_events.7.t', '诺斯信仰觉醒'),
    ('order_events.2', 'order_events.2.t', '女巫之锤'),
    ('overextension.1', 'EVTNAME748', '部队逃亡'),
    ('overextension.10', 'EVTNAME756', '顾问辞去'),
    ('overextension.2', 'EVTNAME749', '普遍的敌对'),
    ('overextension.3', 'EVTNAME750', '败坏名声！'),
    ('overextension.4', 'EVTNAME751', '通货膨胀'),
    ('overextension.5', 'EVTNAME752', '军官逃亡'),
    ('overextension.8', 'EVTNAME754', '正统性问题'),
    ('overextension.9', 'EVTNAME755', '事务官辞职'),
    ('1084', 'EVTNAME1084', '可能的渗入者'),
    ('parlaments.2', 'parlaments.2.t', '腐败选区'),
    ('parlaments.23', 'parlaments.23.t', '要求代表权'),
    ('parlaments.25', 'parlaments.25.t', '来自人的要求'),
    ('parlaments.3', 'parlaments.2.t', '腐败选区'),
    ('promoting_art_events.3', 'promoting_art_events.3.t', '艺术家要求额外的资金'),
    ('promoting_art_events.7', 'promoting_art_events.7.t', '艺术家要求额外的资源'),
    ('personality_events.23', 'personality_events.23.t', '以人为鉴'),
    ('personality_events.24', 'personality_events.24.t', '活到老，学到老？'),
    ('personality_events.31', 'personality_events.31.t', '严守成规'),
    ('personality_events.40', 'personality_events.40.t', '严格的礼仪'),
    ('personality_events.56', 'personality_events.56.t', '咨询顾问'),
    ('pirates.17', 'pirates.17.t', '走私者苦苦挣扎'),
    ('pirates.19', 'pirates.19.t', '船员被列入黑名单'),
    ('polynesian.1', 'polynesian.1.t', '圣地需要清理'),
    ('polynesian.10', 'polynesian.10.t', '甜蜜的威胁'),
    ('polynesian.2', 'polynesian.2.t', '舞蹈出错'),
    ('polynesian.24', 'polynesian.24.t', '火山爆发'),
    ('polynesian.27', 'polynesian.27.t', '凯提阿基塔雅'),
    ('polynesian.3', 'polynesian.3.t', '拂尘不见了'),
    ('polynesian.32', 'polynesian.32.t', '怯懦的继承人'),
    ('polynesian.39', 'polynesian.39.t', '王丘受辱'),
    ('polynesian.40', 'polynesian.40.t', '斐济独木战船'),
    ('polynesian.8', 'polynesian.8.t', '家猪战争'),
    ('prices.25', 'prices.25.t', '孟加拉染料作物种植园'),
    ('privateers.1', 'privateers.1.t', '当地的商人请求援助'),
    ('privateers.5', 'privateers.5.t', '私掠船船长成为了海盗'),
    ('propagate_religion_events.9', 'propagate_religion_events.9.t', '穆斯林酋长'),
    ('2001', 'EVTNAME2001', '新教改革'),
    ('protestantism_events.11', 'protestantism_events.11.t', '谴责教会'),
    ('protestantism_events.12', 'protestantism_events.12.t', '宫廷里的天主教思想家'),
    ('protestantism_events.13', 'protestantism_events.13.t', '的影响'),
    ('protestantism_events.21', 'protestantism_events.21.t', '麻烦的教士'),
    ('protestantism_events.26', 'protestantism_events.26.t', '越权的法庭'),
    ('protestantism_events.28', 'protestantism_events.28.t', '圣礼仪式'),
    ('protestantism_events.31', 'protestantism_events.31.t', '敌对出版物的流通'),
    ('protestantism_events.35', 'protestantism_events.35.t', '爱国而魅力非凡的传教士'),
    ('protestantism_events.7', 'protestantism_events.7.t', '的「末日」'),
    ('purple_phoenix.12', 'purple_phoenix.12.t', '宫廷派系影响统治者'),
    ('purple_phoenix.14', 'purple_phoenix.14.t', '宫廷派系影响统治者'),
    ('purple_phoenix.16', 'purple_phoenix.16.t', '宫廷派系影响统治者'),
    ('3097', 'EVTNAME3097', '起义！'),
    ('5026', 'EVTNAME5026', '瘟疫！'),
    ('5045', 'EVTNAME5045', '政策失当'),
    ('5052', 'EVTNAME5052', '农民不满'),
    ('5053', 'EVTNAME5053', '商人团不满。'),
    ('5059', 'EVTNAME5059', '贸易公司破产'),
    ('5063', 'EVTNAME5063', '贵族冲突'),
    ('5064', 'EVTNAME5064', '贵族通外'),
    ('5071', 'EVTNAME5071', '贵族要求津贴'),
    ('5075', 'EVTNAME5075', '贵族要求旧有权利'),
    ('5076', 'EVTNAME5076', '城市要求旧有权利'),
    ('5077', 'EVTNAME5077', '政令不行'),
    ('5082', 'EVTNAME5082', '要求特权'),
    ('5089', 'EVTNAME5089', '农民请愿'),
    ('5090', 'EVTNAME5090', '贵族要求援助'),
    ('5091', 'EVTNAME5091', '支持流亡的异见者'),
    ('5093', 'EVTNAME5093', '议会要求开出顾问'),
    ('5095', 'EVTNAME5095', '急功近利的顾问'),
    ('5097', 'EVTNAME5097', '农民不满'),
    ('6420', 'EVTNAME6420', '葡萄年成不佳'),
    ('6422', 'EVTNAME6422', '劣质的制服'),
    ('6424', 'EVTNAME6424', '冷漠的原住民'),
    ('6425', 'EVTNAME6425', '食盐危机'),
    ('6427', 'EVTNAME6427', '劣质铜矿'),
    ('6429', 'EVTNAME6429', '劣质铁矿'),
    ('6432', 'EVTNAME6432', '倾茶事件'),
    ('6433', 'EVTNAME6433N', '风暴'),
    ('6434', 'EVTNAME6434', '香料歉收'),
    ('711', 'EVTNAME711', '神甫宣扬异端！'),
    ('713', 'EVTNAME713', '不思进取'),
    ('715', 'EVTNAME715', '使宫廷窘迫'),
    ('716', 'EVTNAME716', '贵族要求报答'),
    ('719', 'EVTNAME719', '强大的军事领导层'),
    ('720', 'EVTNAME720', '海军传统'),
    ('723', 'EVTNAME723', '地方挑战'),
    ('726', 'EVTNAME726', '的声誉受损！'),
    ('730', 'EVTNAME730', '无用的陆军科技研究'),
    ('731', 'EVTNAME731', '海军研究的错误！'),
    ('732', 'EVTNAME732', '新的贸易研究是异端行为'),
    ('733', 'EVTNAME733', '有缺陷的工业研究'),
    ('734', 'EVTNAME734', '应该建立一个稳定的政府'),
    ('735', 'EVTNAME735', '懦夫的一代'),
    ('738', 'EVTNAME738', '天真的贵族'),
    ('740', 'EVTNAME740', '走私猖獗'),
    ('806', 'EVTNAME806', '国家破坏政府稳定'),
    ('836', 'EVTNAME836', '海军思潮的折磨'),
    ('837', 'EVTNAME837', '海军军官退役'),
    ('855', 'EVTNAME855', '士兵不学无术'),
    ('858', 'EVTNAME858', '失败的外交！'),
    ('859', 'EVTNAME859', '厌战的民众'),
    ('877', 'EVTNAME877', '汗热病在传播'),
    ('random_event.15', 'random_event.EVTNAME15', '滑膛枪还是弓箭？'),
    ('random_event.23', 'random_event.EVTNAME23', '流感！'),
    ('random_event.24', 'random_event.EVTNAME24', '流感蔓延'),
    ('random_event.25', 'random_event.EVTNAME25', '圣像破坏运动'),
    ('random_event.26', 'EVTNAME9478', '贬值的货币'),
    ('random_event.27', 'random_event.27.t', '大火'),
    ('random_event.3', 'random_event.EVTNAME3', '议会中的叛徒'),
    ('757', 'EVTNAME757', ' 发生叛乱！'),
    ('2011', 'EVTNAME2011', '改革宗派传播'),
    ('reformed_events.7', 'reformed_events.EVTNAME7', '圣像破坏运动'),
    ('reforms.1', 'reforms.1.t', '金融改革'),
    ('reforms.2', 'reforms.2.t', '社会改革'),
    ('republic_factions.101', 'republic_factions.101.t', '发生革命'),
    ('republic_factions.102', 'republic_factions.102.t', '发生革命'),
    ('republic_factions.103', 'republic_factions.103.t', '发生革命'),
    ('republic_factions.15', 'republic_factions.15.t', '核心集团'),
    ('republic_factions.16', 'republic_factions.16.t', '一次有益的交流'),
    ('republic_factions.17', 'republic_factions.17.t', '血浓于水'),
    ('republic_factions.21', 'republic_factions.21.t', '行会阻碍贸易'),
    ('republic_factions.23', 'republic_factions.23.t', '贵族拒绝共和'),
    ('republic_factions.9', 'republic_factions.9.t', '候选人激烈竞争'),
    ('republican_dictatorship.5', 'republican_dictatorship.5.t', '人民需要民主'),
    ('republican_dictatorship.9', 'republican_dictatorship.9.t', '亲戚无法统治地方'),
    ('republics.10', 'republics.10.t', '议会中的分歧'),
    ('republics.11', 'republics.11.t', '影响政府的争端'),
    ('republics.12', 'republics.12.t', '贸易战争'),
    ('republics.13', 'republics.13.t', '典礼中断'),
    ('republics.17', 'EVTNAME9553', '议会中的争端'),
    ('republics.20', 'EVTNAME9556', '激烈的辩论'),
    ('republics.22', 'EVTNAME9558', '意见冲突'),
    ('republics.25', 'EVTNAME9561', '政治混乱'),
    ('republics.26', 'EVTNAME9562', '受影响的决议'),
    ('republics.40', 'EVTNAME9576', '超支'),
    ('republics.5', 'republics.5.t', '新钱币'),
    ('republics.9', 'republics.9.t', '贵族密谋'),
    ('revolutionary_republic_factions.1', 'revolutionary_republic_factions.1.t', '失牧之群'),
    ('revolutionary_republic_factions.7', 'revolutionary_republic_factions.7.t', '缺少行政人员'),
    ('revolutionary_republic_factions.8', 'revolutionary_republic_factions.8.t', '名将'),
    ('revolutionary_republic_factions.9', 'revolutionary_republic_factions.9.t', '大屠杀'),
    ('russian_principality_events.5', 'russian_principality_events.5.t', '年饥荒'),
    ('russian_principality_events.7', 'russian_principality_events.7.t', '圣乔治日'),
    ('sailor_event.2', 'sailor_event.2.t', '海上苦旅'),
    ('seven_cities_events.101', 'seven_cities_events.101.t', '要么啃皮革，要么死掉'),
    ('seven_cities_events.102', 'seven_cities_events.102.t', '土著袭来之时'),
    ('seven_cities_events.104', 'seven_cities_events.104.t', '神隐'),
    ('seven_cities_events.307', 'seven_cities_events.307.t', '牙龈出血和牙齿脱落'),
    ('seven_cities_events.309', 'seven_cities_events.309.t', '发生在的哗变！'),
    ('seven_cities_events.407', 'seven_cities_events.407.t', '的钻石是假的'),
    ('seven_cities_events.414', 'seven_cities_events.414.t', '一片死寂的湖'),
    ('shinto_events.45', 'shinto_events.45.t', '基督徒群体遇袭'),
    ('siberian_events.3', 'siberian_events.3.t', '渔猎人'),
    ('sikhism_flavor.1', 'sikhism_flavor.1.t', '锡克信仰'),
    ('sikhism_flavor.13', 'sikhism_flavor.13.t', '的上师'),
    ('special_regency_events.5', 'special_regency_events.5.t', '继承人的教育'),
    ('subject_interaction_events.17', 'subject_interaction_events.17.t', '战争从未改变'),
    ('subject_interaction_events.20', 'subject_interaction_events.20.t', '挥霍无度的效忠派'),
    ('subject_interaction_events.21', 'subject_interaction_events.21.t', '效忠派造成窘局'),
    ('subject_interaction_events.23', 'subject_interaction_events.23.t', '效忠派引发叛乱'),
    ('subject_interaction_events.35', 'subject_interaction_events.35.t', '不忠诚的属国'),
    ('tyw_events.11', 'tyw_events.11.t', '胡格诺派信徒聚集在拉罗谢尔'),
    ('tyw_events.17', 'tyw_events.17.t', '华伦斯坦背叛了我们'),
    ('tyw_events.19', 'tyw_events.19.t', '冬季围城'),
    ('tyw_events.22', 'tyw_events.22.t', '战役'),
    ('tyw_events.23', 'tyw_events.23.t', '同盟之敌'),
    ('tyw_events.25', 'tyw_events.25.t', '信徒质疑我们的统治'),
    ('tyw_events.28', 'tyw_events.28.t', '贵族出逃国外'),
    ('tyw_events.40', 'tyw_events.40.t', '和平是上帝的旨意'),
    ('tyw_events.5', 'tyw_events.5.t', '封锁'),
    ('tengri_events.13', 'tengri_events.13.t', '宫廷骚乱'),
    ('tengri_events.15', 'tengri_events.15.t', '城市阶层与游牧民的冲突'),
    ('tengri_events.23', 'tengri_events.23.t', '的偶像破坏'),
    ('tengri_events.3', 'tengri_events.3.t', '勇士失去他们的遗产'),
    ('tengri_events.31', 'tengri_events.31.t', '的担忧'),
    ('tengri_events.33', 'tengri_events.33.t', ''),
    ('tengri_events.36', 'tengri_events.36.t', '颜面尽失'),
    ('tengri_events.45', 'tengri_events.45.t', '宫廷生活'),
    ('tengri_events.46', 'tengri_events.46.t', '婆罗门与萨满'),
    ('tengri_events.5', 'tengri_events.5.t', '危机'),
    ('tengri_events.54', 'tengri_events.54.t', '奢侈品限制法令？'),
    ('tengri_events.62', 'tengri_events.62.t', '保护朝圣者'),
    ('tengri_events.63', 'tengri_events.63.t', '和卓的威望'),
    ('papacy_events.13', 'papacy_events.13.t', '装饰西斯廷礼拜堂'),
    ('totemism_flavor.11', 'totemism_flavor.11.t', '传说终结之处'),
    ('totemism_flavor.7', 'totemism_flavor.7.t', '失声'),
    ('trade.1', 'trade.1.t', '尊重我们的旗帜'),
    ('trade.11', 'trade.11.t', '商人流失'),
    ('trade.2', 'trade.2.t', '是可忍孰不可忍'),
    ('trade.4', 'trade.4.t', '贸易危机'),
    ('trade_company_events.1', 'trade_company_events.EVTNAME1', '重新安置'),
    ('trade_company_events.11', 'trade_company_events.EVTNAME11', '贸易公司遇到麻烦'),
    ('trade_league.1', 'trade_league.1.t', '山丘之王'),
    ('trade_league.12', 'trade_league.12.t', '拒绝票据！'),
    ('trade_league.2', 'trade_league.2.t', '富人更富'),
    ('trade_league.4', 'trade_league.4.t', '海盗闻风而动'),
    ('trade_policy_events.11', 'trade_policy_events.11.t', '对需求的短缺'),
    ('tribal_federation_events.1', 'tribal_federation_events.1.t', '的部族'),
    ('tribal_federation_events.7', 'tribal_federation_events.7.t', '宗室'),
    ('tribal_federation_events.8', 'tribal_federation_events.8.t', '被偷窃的'),
    ('9202', 'EVTNAME9202', '软弱的。'),
    ('9203', 'EVTNAME9203', '家是一个爱好和平的家族'),
    ('9204', 'EVTNAME9204', '王朝失去了部落的尊敬'),
    ('tributary_events.18', 'tributary_events.18.t', '朝贡船队受海盗困扰'),
    ('tributary_events.26', 'tributary_events.26.t', '劫掠'),
    ('tributary_events.30', 'tributary_events.30.t', '朝贡国正与宿敌开战'),
    ('tributary_events.9', 'tributary_events.9.t', '管理朝贡体系'),
    ('usa_dlc.40', 'usa_dlc.EVTNAME40', '纽约大火'),
    ('west_african.18', 'west_african.18.t', '曾经的游牧民族'),
    ('zoroastrian.1', 'zoroastrian.1.t', '桑占的帕西人避难所'),
    ('center_of_revolution.13', 'center_of_revolution.13.t', '自由理想'),
    ('center_of_revolution.2001', 'center_of_revolution.2001.name', '面粉战争'),
    ('center_of_revolution.8', 'center_of_revolution.8.name', '的反革命分子'),
    ('revolution.13', 'EVTNAME6604', '革命终止'),
    ('revolution.16', 'EVTNAME6607', '王室复辟'),
    ('french_wars_of_religion.13', 'flavor_fra.EVTNAME3196', '王都陷落！'),
    ('french_wars_of_religion.6', 'flavor_fra.EVTNAME3200', '宗教狂热'),
    ('french_wars_of_religion.8', 'flavor_fra.EVTNAME3202', '狂热异端分子'),
    ('dai_disaster.3', 'dai_disaster.3.t', '在的王朝紧张局势'),
    ('plot_of_the_harem_events.6', 'plot_of_the_harem_events.6.t', '试探皇权的极限'),
    ('brahmins_estate_events.2', 'brahmins_estate_events.2.t', '婆罗门要求朝廷职位正当化'),
    ('burghers_estate_events.10', 'burghers_estate_events.10.t', '社会流动性'),
    ('burghers_estate_events.14', 'burghers_estate_events.14.t', '经济政策'),
    ('church_estate_events.6', 'church_estate_events.6.t', '的崛起？'),
    ('cossack_estate_events.14', 'cossack_estate_events.14.t', '索要拓宽特权'),
    ('dhimmi_estate_events.1', 'dhimmi_estate_events.1.t', '过分狂热的卡迪'),
    ('dhimmi_estate_events.7', 'dhimmi_estate_events.7.t', '异教匪徒'),
    ('janissaries_estate_events.4', 'janissaries_estate_events.4.t', '耶尼切里领地所有权'),
    ('janissaries_estate_events.8', 'janissaries_estate_events.8.t', '耶尼切里反叛！'),
    ('janissaries_estate_events.9', 'janissaries_estate_events.9.t', '耶尼切里提出要求'),
    ('estate_regency_events.6', 'estate_regency_events.6.t', '宗教偏狭加深'),
    ('estate_regency_events.8', 'estate_regency_events.8.t', '鬼影幢幢'),
    ('marathas_estate_events.4', 'marathas_estate_events.4.t', '为巴尔吉尔准备的马匹'),
    ('nobles_estate_events.3', 'nobles_estate_events.3.t', '侵占的土地'),
    ('nobles_estate_events.6', 'nobles_estate_events.6.t', '要求颁布更严格的奢侈品禁令'),
    ('nobles_estate_events.9', 'nobles_estate_events.9.t', '的违抗'),
    ('tribal_estate_events.11', 'tribal_estate_events.11.t', '大位的继承'),
    ('tribal_estate_events.12', 'tribal_estate_events.12.t', '部落要求更多的人力'),
    ('tribal_estate_events.14', 'tribal_estate_events.14.t', '的要求'),
    ('flavor_ayu.4', 'flavor_ayu.EVTNAME4', '无能的·'),
    ('flavor_azt.14', 'flavor_azt.14.t', '大瘟疫'),
    ('flavor_azt.5', 'flavor_azt.5.t', '兔一年的饥荒'),
    ('flavor_bah.1', 'flavor_bah.1.t', '比德尔，巴赫曼尼实际意义上的首都'),
    ('flavor_bah.5', 'flavor_bah.5.t', '德干派和西方派'),
    ('new_flavour_bav.25', 'new_flavour_bav.25.name', '我们的债务被豁免了'),
    ('new_flavour_bav.27', 'new_flavour_bav.27.name', '丹麦统治下的暗流'),
    ('flavor_bng.6', 'flavor_bng.6.t', '猛虎崛起'),
    ('flavor_bng.7', 'flavor_bng.7.t', '布尔修特罗阇'),
    ('flavor_boh.3', 'flavor_boh.3.t', '泽列纳霍拉同盟'),
    ('flavor_brapru.30', 'flavor_brapru.30.t', '柯尼斯堡的市民'),
    ('flavor_brapru.33', 'flavor_brapru.33.t', '议会休会'),
    ('flavor_brapru.40', 'flavor_brapru.40.t', '卷土重来的强盗贵族'),
    ('MEE_Byzantine_Events.47', 'MEE_Byzantine_Events.47.t', '反教会归一分子叛乱'),
    ('flavor_che.2', 'flavor_che.2.t', '复仇'),
    ('flavor_che.3', 'flavor_che.3.t', '与欧洲人的接触'),
    ('flavor_dai.2', 'flavor_dai.EVTNAME2', '南北朝'),
    ('flavor_dan.1', 'flavor_dan.1.t', '第二次大洪水'),
    ('flavor_dan.115', 'flavor_dan.115.t', '自由之梦'),
    ('flavor_dan.9', 'flavor_dan.9.t', '鲱鱼逐渐消失'),
    ('kalmar_union_events.10', 'kalmar_union_events.10.t', '卡尔马联盟的陨落'),
    ('kalmar_union_events.7', 'kalmar_union_events.7.t', '篡位者发动叛乱'),
    ('flavor_dlh.3', 'flavor_dlh.EVTNAME3', '洛迪王朝'),
    ('flavor_daimyo.10', 'flavor_daimyo.10.t', '明历大火'),
    ('flavor_daimyo.11', 'flavor_daimyo.11.t', '南海海啸'),
    ('flavor_daimyo.13', 'flavor_daimyo.13.t', '富士山宝永大喷发'),
    ('flavor_daimyo.15', 'flavor_daimyo.15.t', '岛原之乱'),
    ('flavor_daimyo.20', 'flavor_daimyo.20.t', '晴信叛乱'),
    ('flavor_daimyo.23', 'flavor_daimyo.23.t', '信长的野望'),
    ('flavor_daimyo.5', 'flavor_daimyo.5.t', '合理的继承'),
    ('flavor_daimyo.6', 'flavor_daimyo.6.t', '的继承权'),
    ('flavor_daimyo.8', 'flavor_daimyo.8.t', '扇谷家'),
    ('flavor_eth.23', 'flavor_eth.23.t', '奥罗莫定居者'),
    ('flavor_eth.28', 'flavor_eth.28.t', '王山的灾难'),
    ('flavor_eth.52', 'flavor_eth.52.t', '的耶稣会活动'),
    ('flavor_ful.1', 'flavor_ful.1.t', '隐士在发动叛乱'),
    ('flavor_gen.8', 'flavor_gen.8.t', '圣乔治银行破产'),
    ('flavor_guj.4', 'flavor_guj.4.t', '尚庞沦陷'),
    ('flavor_hol.10', 'flavor_hol.10.t', '联合东印度公司的困境'),
    ('flavor_hol.11', 'flavor_hol.11.t', '经济危机中的联合东印度公司'),
    ('flavor_hol.12', 'flavor_hol.12.t', '联合东印度公司国有化？'),
    ('flavor_hol.13', 'flavor_hol.13.t', '阿姆斯特丹银行与国际资本市场'),
    ('flavor_hol.211', 'flavor_hol.211.t', '分食'),
    ('flavor_hol.22', 'flavor_hol.22.t', '阿鲁姆黑军起义'),
    ('flavor_hol.23', 'flavor_hol.23.t', '假发时期'),
    ('flavor_hol.3508', 'flavor_hol.EVTNAME3508', '尼德兰的加尔文宗'),
    ('flavor_hol.3515', 'flavor_hol.EVTNAME3515', '荷兰革命'),
    ('flavor_hol.6', 'flavor_hol.6.t', '郁金香狂热'),
    ('flavor_hun.15', 'flavor_hun.15.t', '上匈牙利的匪患'),
    ('flavor_hun.19', 'flavor_hun.19.t', '雇佣兵哗变'),
    ('flavor_hun.40', 'flavor_hun.40.t', '古贝茨农民起义'),
    ('flavor_hun.49', 'flavor_hun.49.t', '尤拉伊·亚诺希克'),
    ('flavor_hun.50', 'flavor_hun.50.t', '亚诺希克再次来袭'),
    ('flavor_hun.8', 'flavor_hun.8.t', '匈牙利吸血鬼'),
    ('flavor_inc.11', 'flavor_inc.11.t', '之死'),
    ('flavor_iro.1', 'flavor_iro.1.t', '不可饶恕的公然冒犯'),
    ('flavor_jap.17', 'flavor_jap.EVTNAME17', '东山文化'),
    ('flavor_jap.32', 'flavor_jap.EVTNAME32', '刀狩－糟糕的执行者'),
    ('flavor_jap.42', 'flavor_jap.EVTNAME42', '颜面大失'),
    ('flavor_jap.45', 'flavor_jap.EVTNAME45', '献给天皇的漆器'),
    ('flavor_jap.7', 'flavor_jap.EVTNAME7', '南蛮贸易'),
    ('flavor_jap.71', 'flavor_jap.71.t', '明历大火'),
    ('flavor_kon.10', 'flavor_kon.10.t', '皈依天主教！'),
    ('flavor_kor.10', 'flavor_kor.10.t', '士林派的崛起'),
    ('flavor_kor.2', 'flavor_kor.EVTNAME2', '第一次士祸'),
    ('flavor_kor.3', 'flavor_kor.EVTNAME3', '第二次士祸'),
    ('flavor_kor.8', 'flavor_kor.8.t', '朝鲜农民暴动'),
    ('flavor_kor.9', 'flavor_kor.9.t', '林巨正起事'),
    ('flavor_lit.12', 'flavor_lit.EVTNAME12', '可怕的瘟疫流行于立陶宛'),
    ('flavor_lit.19', 'flavor_lit.19.t', '赫梅利尼茨基起义'),
    ('flavor_mal.5', 'flavor_mal.5.t', '葡萄牙干预'),
    ('flavor_mam.13', 'flavor_mam.13.t', '开罗瘟疫'),
    ('flavor_mam.15', 'flavor_mam.15.t', '葡萄牙－马穆鲁克贸易战争'),
    ('flavor_mam.9', 'flavor_mam.9.t', '马穆鲁克贵族'),
    ('flavor_mca.1', 'flavor_mca.1.t', '西帕和萨克'),
    ('flavor_mer.5', 'flavor_mer.5.t', '针对俱摩婆王的阴谋'),
    ('flavor_mng.11', 'flavor_mng.EVTNAME11', '财政危机'),
    ('flavor_mng.13', 'flavor_mng.EVTNAME13', '驱逐耶稣会士'),
    ('flavor_mng.50', 'flavor_mng.50.t', '八虎'),
    ('flavor_mng.54', 'flavor_mng.54.t', '年地震'),
    ('flavor_mng.57', 'flavor_mng.57.t', '帝国的宦官'),
    ('flavor_mng.8', 'flavor_mng.EVTNAME8', '饥荒！'),
    ('flavor_mng.9', 'flavor_mng.EVTNAME9', '满洲之乱'),
    ('flavor_mol.1', 'flavor_mol.EVTNAME1', '支持摩尔达维亚的罗曼'),
    ('flavor_mor.8', 'flavor_mor.8.t', '瓦塔斯家族的最后通牒'),
    ('flavor_mos.7', 'flavor_mos.7.t', '莫斯科内战第三阶段'),
    ('flavor_mug.3', 'flavor_mug.EVTNAME3', '锡克教徒骚乱'),
    ('flavor_mug.4', 'flavor_mug.EVTNAME4', '阿富汗叛乱'),
    ('flavor_nap.5', 'flavor_nap.5.t', '马萨涅洛起义'),
    ('flavor_nap.6', 'flavor_nap.6.t', '那不勒斯的瘟疫'),
    ('flavor_nor.11', 'flavor_nor.11.t', '卑尔根骚乱'),
    ('flavor_ori.3', 'flavor_ori.3.t', '迦毗莲陀罗驾崩'),
    ('flavor_pol.3467', 'flavor_pol.EVTNAME3467', '涅沙瓦特权——特别协议'),
    ('flavor_pol.3493', 'flavor_pol.EVTNAME3493', '可怕的瘟疫流行于立陶宛'),
    ('flavor_pol.3738', 'flavor_pol.EVTNAME3738', '瑟姆议会拒绝动员更多的军队'),
    ('flavor_pol.3744', 'flavor_pol.EVTNAME3744', '瑟姆议会否决所有政策'),
    ('flavor_sax.7', 'flavor_sax.7.name', '萨克森王子被劫为人质'),
    ('flavor_sco.4', 'flavor_sco.EVTNAME4', '约翰·诺克斯与宗教冲突'),
    ('flavor_sio.2', 'flavor_sio.2.t', '往生极乐猎场'),
    ('flavor_sio.4', 'flavor_sio.4.t', '不速之客'),
    ('flavor_sok.1', 'flavor_sok.1.t', '奥斯曼·丹·福迪奥'),
    ('flavor_son.1', 'flavor_son.1.t', '廷巴克图的神职人员'),
    ('flavor_son.4', 'flavor_son.4.t', '穆罕默德的崛起'),
    ('flavor_teu.22', 'flavor_teu.22.t', '德意志农民战争'),
    ('flavor_tim.17', 'flavor_tim.17.t', '滥用权力'),
    ('flavor_tim.21', 'flavor_tim.21.t', '伊斯法罕的行动'),
    ('flavor_tus.20', 'flavor_tus.20.t', '帕齐阴谋'),
    ('flavor_tupi.16', 'flavor_tupi.16.t', '卡拉穆鲁的欧洲之旅'),
    ('flavor_vie.1', 'flavor_vie.EVTNAME1', '继承危机'),
    ('flavor_vij.11', 'flavor_vij.11.t', '泰米尔贸易工会'),
    ('flavor_zan.4', 'flavor_zan.4.t', '食人族！'),
    ('flavor_zan.7', 'flavor_zan.7.t', '炮制阴谋的维齐尔'),
    ('flavor_zim.1', 'flavor_zim.1.t', '津巴布韦的衰退'),
    ('flavor_zim.9', 'flavor_zim.9.t', '沦陷！'),
    ('ideagroups.100', 'EVTNAME803', '损失稳定'),
    ('ideagroups.1001', 'EVTNAME6101', '人民对海军事业的热情削减'),
    ('ideagroups.1003', 'EVTNAME6103', '贵族表示担忧'),
    ('ideagroups.1005', 'EVTNAME6105', '傲慢的海军'),
    ('ideagroups.1007', 'EVTNAME6107', '海军纪律废弛'),
    ('ideagroups.1009', 'EVTNAME6109', '关心造船工人'),
    ('ideagroups.101', 'EVTNAME804', '市民造反！'),
    ('ideagroups.1011', 'EVTNAME6111', '海军素质下降'),
    ('ideagroups.1012', 'EVTNAME839', '军械库荒废'),
    ('ideagroups.1013', 'EVTNAME840', '将军退休'),
    ('ideagroups.103', 'EVTNAME812', '农民起义'),
    ('ideagroups.105', 'EVTNAME814', '严重叛乱'),
    ('ideagroups.1101', 'EVTNAME6145', '产量滞涩'),
    ('ideagroups.1103', 'EVTNAME6147', '腐败的官僚机构'),
    ('ideagroups.1107', 'EVTNAME6149', '国家银行的问题'),
    ('ideagroups.1204', 'EVTNAME6143', '腐败的总督'),
    ('ideagroups.1210', 'ideagroups.1210.t', '歉收'),
    ('ideagroups.1300', 'EVTNAME842', '不合格的新兵'),
    ('ideagroups.1301', 'EVTNAME843', '贵族的担忧'),
    ('ideagroups.1305', 'EVTNAME6307', '兵员危机'),
    ('ideagroups.1400', 'EVTNAME845', '人们嘲笑军队'),
    ('ideagroups.1401', 'EVTNAME846', '将领难寻'),
    ('ideagroups.1409', 'ideagroups.1409.t', '木材短缺'),
    ('ideagroups.1410', 'ideagroups.1410.t', '管理问题'),
    ('ideagroups.1508', 'ideagroups.1508.t', '贸易失败'),
    ('ideagroups.1509', 'ideagroups.1509.t', '造船问题'),
    ('ideagroups.1510', 'ideagroups.1510.t', '外交失言'),
    ('ideagroups.1601', 'EVTNAME6335', '省份因缺乏自主权而沮丧'),
    ('ideagroups.1602', 'EVTNAME6337', '省份要求更多自主权'),
    ('ideagroups.1609', 'ideagroups.1609.t', '失败的管理'),
    ('ideagroups.1610', 'ideagroups.1610.t', '付款要求'),
    ('ideagroups.1707', 'ideagroups.1707.t', '海上灾难'),
    ('ideagroups.1708', 'ideagroups.1708.t', '兵源流失'),
    ('ideagroups.1709', 'ideagroups.1709.t', '木材腐烂'),
    ('ideagroups.1806', 'ideagroups.1806.t', '真理在大炮射程之内'),
    ('ideagroups.1809', 'ideagroups.1809.t', '粗鲁的要求'),
    ('ideagroups.1810', 'ideagroups.1810.t', '敌意言论'),
    ('ideagroups.1907', 'ideagroups.1907.t', '缰绳过松'),
    ('ideagroups.1909', 'ideagroups.1909.t', '传教工作徒劳无功'),
    ('ideagroups.1915', 'ideagroups.1915.t', '光芒的消逝'),
    ('ideagroups.1916', 'ideagroups.1916.t', '尊贵之人亦当怀疑'),
    ('ideagroups.1926', 'ideagroups.1926.t', '沉默的羔羊'),
    ('ideagroups.1927', 'ideagroups.1927.t', '精神错乱'),
    ('ideagroups.1935', 'ideagroups.1935.t', '不敬先祖'),
    ('ideagroups.1938', 'ideagroups.1938.t', '失控的大火'),
    ('ideagroups.1941', 'ideagroups.1941.t', '新观念并不适合所有人'),
    ('ideagroups.200', 'EVTNAME800', '贵族起义'),
    ('ideagroups.2000', 'ideagroups.2000.t', '乡村道路年久失修'),
    ('ideagroups.2003', 'ideagroups.2011.t', '建筑物资不足'),
    ('ideagroups.2004', 'ideagroups.2020.t', '的发展放慢了'),
    ('ideagroups.201', 'EVTNAME801', '损失威望'),
    ('ideagroups.2013', 'ideagroups.2013.t', '宫廷混乱'),
    ('ideagroups.2017', 'ideagroups.2017.t', '宫廷需要翻新'),
    ('ideagroups.2021', 'ideagroups.2005.t', '将军们的抱怨'),
    ('ideagroups.2028', 'ideagroups.2028.t', '雇佣兵的声誉'),
    ('ideagroups.204', 'EVTNAME6157', '强烈反对'),
    ('ideagroups.205', 'EVTNAME815', '人们不见了'),
    ('ideagroups.206', 'EVTNAME816', '失控'),
    ('ideagroups.209', 'EVTNAME6303', '贵族的担忧'),
    ('ideagroups.300', 'EVTNAME818', '危险的思想正在蔓延！'),
    ('ideagroups.301', 'EVTNAME819', '孤芳自赏'),
    ('ideagroups.304', 'EVTNAME6194', '缺乏值得赞助的艺术杰作'),
    ('ideagroups.306', 'EVTNAME6192', '科学革命中断'),
    ('ideagroups.401', 'EVTNAME822', '骚乱'),
    ('ideagroups.404', 'EVTNAME6196', '被打破的承诺'),
    ('ideagroups.406', 'EVTNAME6159', '宗教分歧'),
    ('ideagroups.408', 'EVTNAME6161', '保守派不满'),
    ('ideagroups.410', 'EVTNAME6333', '自由主义者的愤怒'),
    ('ideagroups.501', 'EVTNAME6153', '间谍的背叛'),
    ('ideagroups.503', 'ideagroups.503.t', '我们的活动是隐蔽的……'),
    ('ideagroups.504', 'ideagroups.504.t', '更加高效地支持叛军'),
    ('ideagroups.505', 'ideagroups.505.t', '外交官暴露'),
    ('ideagroups.507', 'ideagroups.507.t', '谍报行动耗费精力过多！'),
    ('ideagroups.601', 'EVTNAME6155', '腐败的政府'),
    ('ideagroups.603', 'ideagroups.603.t', '不称职的外交官'),
    ('ideagroups.607', 'ideagroups.607.t', '喋喋不休的小丑'),
    ('ideagroups.700', 'EVTNAME830', '贵族的担忧'),
    ('ideagroups.701', 'EVTNAME831', '太多胆小鬼'),
    ('ideagroups.704', 'EVTNAME6113', '国民不想被强征入伍'),
    ('ideagroups.706', 'EVTNAME6115', '我们的军队不再强大'),
    ('ideagroups.708', 'EVTNAME6313', '要塞防御被忽视'),
    ('ideagroups.800', 'EVTNAME833', '贵族不愿服役'),
    ('ideagroups.801', 'EVTNAME834', '农夫的烦恼'),
    ('ideagroups.804', 'EVTNAME6131', '贵族忧心丧失特权'),
    ('ideagroups.806', 'EVTNAME6117', '军事上的安于现状'),
    ('ideagroups.808', 'EVTNAME6133', '军队今不如昔'),
    ('ideagroups.810', 'EVTNAME6325', '将军们担心我们的懦弱战术'),
    ('ideagroups.901', 'EVTNAME6141', '自鸣得意的商人'),
    ('ideagroups.903', 'EVTNAME6135', '我国商人表现不佳'),
    ('ideagroups.905', 'EVTNAME6151', '贸易政策失败'),
    ('ideagroups.910', 'ideagroups.910.t', '贸易崩溃'),
    ('institution_events.10', 'institution_events.10.t', '缺乏官僚体系'),
    ('institution_events.26', 'institution_events.26.t', '威信快速丧失'),
    ('institution_events.8', 'institution_events.8.t', '挑战军事豪强'),
    ('6501', 'EVTNAME6501', '进一步妥协'),
    ('randomeventsstapehordes.1', 'randomeventsstapehordes.1.EVTNAME', '生于马背'),
    ('randomeventsstapehordes.14', 'randomeventsstapehordes.14.EVTNAME', '焦躁不安的游牧民'),
    ('randomeventsstapehordes.17', 'randomeventsstapehordes.17.EVTNAME', '部落冲突'),
    ('randomeventsstapehordes.19', 'randomeventsstapehordes.19.EVTNAME', '打猎事故'),
    ('randomeventsstapehordes.20', 'randomeventsstapehordes.20.EVTNAME', '我们看起来虚弱不堪'),
    ('randomeventsstapehordes.21', 'randomeventsstapehordes.21.EVTNAME', '权威削弱'),
    ('randomeventsstapehordes.22', 'randomeventsstapehordes.22.EVTNAME', '军事传统遭到嘲弄'),
    ('randomeventsstapehordes.23', 'randomeventsstapehordes.23.EVTNAME', '军事传统遭到嘲弄'),
    ('randomeventsstapehordes.5', 'randomeventsstapehordes.5.EVTNAME', '敌对部落劫掠'),
)


def fnv1a64(s):
    h = 0xCBF29CE484222325
    for c in s.encode("utf-8"):
        h = ((h ^ c) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def blob_badev():
    hs = sorted({fnv1a64(t) for _, t, _ in BAD_EVENTS})
    return struct.pack("<Q", len(hs)) + b"".join(struct.pack("<Q", h) for h in hs)


BLOBS = {"@badev": blob_badev}

def rel_target(img, insn_rva, disp_off, insn_len):
    return insn_rva + insn_len + struct.unpack("<i", img.bytes_at(insn_rva + disp_off, 4))[0]


def resolve(img):
    """用特征码解析出挂钩位置和机器码要用的全局变量/函数。返回 (表, 问题列表)。"""
    R, problems = {}, []
    # 游戏对象全局指针：取玩家标签的代码被内联在很多地方，命中处的 mov rbx,[rip+x] 必须全部指向同一处
    globs = {rel_target(img, h, 3, 7) for h in img.find_sig(SIGS["PLAYER"])}
    if len(globs) == 1:
        R["glob"] = globs.pop()
    else:
        problems.append(f"玩家标签全局变量: 解码出 {len(globs)} 个不同地址")
    # 战斗骰子面数：掷骰函数里 idiv dword ptr [rip+x]
    hits = img.find_sig(SIGS["DICEFN"])
    if len(hits) == 1 and img.bytes_at(hits[0] + 0x2F, 2) == bytes.fromhex("F73D"):
        R["dside"] = rel_target(img, hits[0] + 0x2F, 2, 6)
    else:
        problems.append("战斗骰子面数: 特征码未命中或不唯一")
    for name, sk, off, pro_len, cave, keys in HOOKS:
        hits = img.find_sig(SIGS[sk])
        if len(hits) != 1:
            problems.append(f"{name}: 特征码{'未命中' if not hits else '命中 %d 处(不唯一)' % len(hits)}")
            continue
        R["entry:" + name] = hits[0] + off
    for name, sk, off, old, new in DIRECT:
        hits = img.find_sig(SIGS[sk])
        if len(hits) == 1 and img.bytes_at(hits[0] + off, len(old) // 2) == bytes.fromhex(old):
            R["entry:" + name] = hits[0] + off
        else:
            problems.append(f"{name}: 特征码{'未命中或原字节不符' if len(hits) <= 1 else '命中 %d 处(不唯一)' % len(hits)}")
    # 原版全歼函数：战斗结束结算里兵力悬殊时调用的那个
    e = R.get("entry:玩家打赢陆战即全歼")
    if e is not None and img.bytes_at(e + 0x162, 1) == b"\xE8":
        R["wipe"] = rel_target(img, e + 0x162, 1, 5)
    elif e is not None:
        problems.append("原版全歼函数: 结算函数里找不到调用")
    # 盟友直接加入战争的分支：同盟分支里“是敌方共同交战国”时跳过去的 je 目标(命中处 +0x25)
    e = R.get("entry:盟友像附庸一样直接参战")
    if e is not None and img.bytes_at(e - 0x30 + 0x25, 1) == b"t":
        R["direct"] = e - 0x30 + 0x27 + img.bytes_at(e - 0x30 + 0x26, 1)[0]
    elif e is not None:
        problems.append("盟友直接加入战争的分支: 找不到")
    return R, problems


def install_all(h, base, img):
    R, problems = resolve(img)
    out = [("特征码问题", p) for p in problems]
    for name, sk, off, old, new in DIRECT:
        entry = R.get("entry:" + name)
        if entry is None:
            out.append((name, "特征码未命中，跳过"))
            continue
        cur = P.read_mem(h, base + entry, len(old) // 2)
        if cur == bytes.fromhex(new):
            out.append((name, "已存在"))
        elif cur != bytes.fromhex(old):
            out.append((name, "进程内指令与文件不符，跳过"))
        else:
            out.append((name, "安装成功" if P.patch_code(h, base + entry, bytes.fromhex(new)) else "写入失败"))
    todo = []
    for name, sk, off, pro_len, cave, keys in HOOKS:
        entry = R.get("entry:" + name)
        if entry is None:
            out.append((name, "特征码未命中，跳过"))
            continue
        if any(k not in R for k in keys if not k.startswith("@")):
            out.append((name, "缺少全局变量/函数，跳过"))
            continue
        pro = img.bytes_at(entry, pro_len)
        cur = P.read_mem(h, base + entry, pro_len)
        if cur != pro:
            out.append((name, "已存在" if cur[:1] == b"\xE9" else "进程内指令与文件不符，跳过"))
            continue
        todo.append((name, entry, pro, cave, keys))
    if not todo:
        return out
    arena = P.alloc_near(h, base + img.text_rva + len(img.text) // 2, 0x4000)
    if not arena:
        return out + [("补丁", "分配内存失败")]
    cur_off = 0
    blob_at = {}
    for k in {k for *_, keys in todo for k in keys if k.startswith("@")}:
        data = BLOBS[k]()
        if not P.write_mem(h, arena + cur_off, data):
            return out + [("补丁", f"写入数据 {k} 失败")]
        blob_at[k] = arena + cur_off
        cur_off += (len(data) + 15) & ~15
    for name, entry, pro, cave, keys in todo:
        e = base + entry
        code = bytearray(bytes.fromhex(CAVES[cave]))
        for i, k in enumerate(keys):
            struct.pack_into("<Q", code, 8 * i, blob_at[k] if k.startswith("@") else base + R[k])
        start = arena + cur_off
        code += pro                                   # 被覆盖的原指令搬进补丁
        jmp_at = len(code)
        code += bytes([0xE9]) + struct.pack("<i", (e + len(pro)) - (start + jmp_at + 5))
        if not P.write_mem(h, start, bytes(code)):
            out.append((name, "写入补丁失败"))
            continue
        patch = bytes([0xE9]) + struct.pack("<i", (start + 8 * len(keys)) - (e + 5)) + b"\x90" * (len(pro) - 5)
        out.append((name, "安装成功" if P.patch_code(h, e, patch) else "写入失败"))
        cur_off += (len(code) + 15) & ~15
    return out


def remove_all(h, base, img):
    R, _ = resolve(img)
    n = 0
    for name, sk, off, old, new in DIRECT:
        entry = R.get("entry:" + name)
        if entry is not None and P.read_mem(h, base + entry, len(new) // 2) == bytes.fromhex(new):
            n += P.patch_code(h, base + entry, bytes.fromhex(old))
    for name, sk, off, pro_len, *_r in HOOKS:
        entry = R.get("entry:" + name)
        if entry is None:
            continue
        pro = img.bytes_at(entry, pro_len)
        cur = P.read_mem(h, base + entry, pro_len)
        if len(cur) == pro_len and cur != pro and P.patch_code(h, base + entry, pro):
            n += 1
    return n


def attach(wait_seconds):
    """等 eu4.exe 出现且代码段可读。返回 (h, base, img, pid) 或 None。"""
    deadline = time.time() + wait_seconds
    img = None
    while time.time() < deadline:
        pid = P.find_pid(EXE)
        if pid:
            try:
                h = P.open_process(pid)
                base = P.module_base(h)
                if img is None:
                    img = Image(P.exe_path(h))
                if P.read_mem(h, base + img.text_rva, 16) == img.text[:16]:
                    return h, base, img, pid
            except OSError:
                pass
        time.sleep(0.5)
    return None


def main():
    global _quiet
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true", help="给运行中的游戏装补丁后退出")
    ap.add_argument("--remove", action="store_true", help="撤销补丁")
    ap.add_argument("--status", action="store_true", help="只显示特征码定位结果，不写入")
    ap.add_argument("--wait-game", type=int, default=1800, help="等待 eu4.exe 启动的秒数(要经过启动器，默认 30 分钟)")
    ap.add_argument("--watch", action="store_true",
                    help="守候模式(Steam 启动项用)：每次 eu4.exe 启动(包括游戏自己重启)都装补丁，游戏退出后没再启动就结束")
    ap.add_argument("--restart-wait", type=int, default=60, help="守候模式下游戏退出后等它重新启动的秒数(默认 1 分钟)")
    args, game_cmd = ap.parse_known_args()
    _quiet = sys.stdout is None or Path(sys.executable).name.lower() == "pythonw.exe"
    if args.status or args.remove:
        a = attach(5 if args.status else args.wait_game)
        if not a:
            log("未找到 eu4.exe")
            return 1
        h, base, img, pid = a
        if args.status:
            R, problems = resolve(img)
            for name, *_ in HOOKS + DIRECT:
                log(f"{name}: {'RVA 0x%X' % R['entry:' + name] if 'entry:' + name in R else '未定位'}")
            for k in ("glob", "dside", "wipe", "direct"):
                log(f"{k}: {'0x%X' % R[k] if k in R else '未找到'}")
            for p in problems:
                log("问题: " + p)
        else:
            log(f"已撤销 {remove_all(h, base, img)} 个补丁")
        return
    if game_cmd:  # Steam 启动：先拉起游戏(启动器)，再起一个等 eu4.exe 出现后安装的进程
        import subprocess
        try:
            subprocess.Popen(game_cmd, cwd=str(Path(game_cmd[0]).parent), creationflags=0x00000008)
        except OSError as e:
            log(f"启动游戏失败: {e!r}；收到的参数: {game_cmd!r}")
            return
        cmd = [sys.executable, str(Path(__file__).resolve()), "--watch", "--wait-game", str(args.wait_game),
               "--restart-wait", str(args.restart_wait)]
        # 守候进程在游戏退出后还会等一会儿：尽量脱离 Steam 的作业对象(CREATE_BREAKAWAY_FROM_JOB)，
        # 免得 Steam 一直显示游戏在运行；不允许脱离时照常启动
        try:
            subprocess.Popen(cmd, creationflags=0x00000008 | 0x08000000 | 0x01000000, close_fds=True)
        except OSError:
            subprocess.Popen(cmd, creationflags=0x00000008 | 0x08000000, close_fds=True)
        log(f"已启动游戏: {game_cmd[0]}")
        return
    if args.watch:
        watch(args.wait_game, args.restart_wait)
        return
    a = attach(args.wait_game)
    if not a:
        log("等待 eu4.exe 超时")
        sys.exit(1)
    h, base, img, pid = a
    res = install_all(h, base, img)
    log(f"补丁结果 (pid {pid}): " + "；".join(f"{n}:{r}" for n, r in res))


def watch(wait_first, restart_wait):
    """守候：游戏在对局中返回主菜单等情况会自己重启 eu4.exe，新进程不经过 Steam 启动项，
    所以这里一直守着，每出现一个新的 eu4.exe 就装一次补丁；游戏退出后 restart_wait 秒内没再启动就结束。"""
    mutex = P.single_instance(r"Local\eu4_buff_watch")
    if mutex is None:
        log("已有守候进程在运行，本进程退出")
        return
    timeout = wait_first
    while True:
        a = attach(timeout)
        if not a:
            log("没有等到 eu4.exe 启动，守候结束")
            return
        h, base, img, pid = a
        res = install_all(h, base, img)
        log(f"补丁结果 (pid {pid}): " + "；".join(f"{n}:{r}" for n, r in res))
        P.wait_exit(h)
        log(f"eu4.exe (pid {pid}) 已退出，等待游戏重新启动")
        timeout = restart_wait


if __name__ == "__main__":
    main()
