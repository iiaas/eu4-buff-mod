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
6. 成就(对所有人生效)：装了 mod、改过的游戏/存档、非铁人模式也能获得成就。
   - 脚本触发器 ironman 恒按“是铁人”判断(成就条件里都写了 ironman = yes)，
     所以其他脚本里的 ironman 判断也会受影响。
   - 难度、AI 难度、幸运国家、开局设置、多人游戏、指定国家等其他成就条件不变。

兼容性
------
- 适用版本：1.37.5.0(EU4 已停止更新)。
- 补丁位置用特征码在 eu4.exe 里查找；某处找不到或不唯一，只跳过那一项并写日志。

用法
----
    Steam 启动项：  pythonw.exe 本脚本 %command%     (启动游戏并自动安装)
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
            if pe.szExeFile.decode(errors="ignore").lower() == name:
                pid = pe.th32ProcessID
                break
            ok = k.Process32Next(snap, ctypes.byref(pe))
        k.CloseHandle(snap)
        return pid

    @staticmethod
    def open_process(pid):
        P.k32.OpenProcess.restype = wintypes.HANDLE
        h = P.k32.OpenProcess(0x1F0FFF, False, pid)
        if not h:
            raise OSError(f"OpenProcess 失败(错误 {ctypes.get_last_error()})")
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
}

# (名称, 特征码键, 挂钩处相对命中处的偏移, 被搬走的原指令长度, 机器码键, 数据区各项)
# 数据区：glob = 游戏对象全局指针，dside = 战斗骰子面数(COMBAT_DICE_SIDE)，wipe = 原版全歼函数，
#         direct = 召集循环里“盟友直接加入战争”的分支
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
        if any(k not in R for k in keys):
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
    for name, entry, pro, cave, keys in todo:
        e = base + entry
        code = bytearray(bytes.fromhex(CAVES[cave]))
        for i, k in enumerate(keys):
            struct.pack_into("<Q", code, 8 * i, base + R[k])
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
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--install", "--wait-game", str(args.wait_game)],
                         creationflags=0x00000008 | 0x08000000, close_fds=True)
        log(f"已启动游戏: {game_cmd[0]}")
        return
    a = attach(args.wait_game)
    if not a:
        log("等待 eu4.exe 超时")
        sys.exit(1)
    h, base, img, pid = a
    res = install_all(h, base, img)
    log(f"补丁结果 (pid {pid}): " + "；".join(f"{n}:{r}" for n, r in res))


if __name__ == "__main__":
    main()
