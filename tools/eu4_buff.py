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
6. 随机池里只留有纯好处的事件：
   - 每次安装时读游戏目录和启用的 mod 的脚本(dlc_load.json)，找出随机触发的事件(on_actions 随机池、各 pulse、
     平均触发时间)，标出每个选项是不是“纯好处”(有好处且没有任何坏处；封装的脚本效果展开再算；接着触发别的
     事件的不算，后续好坏未知；立即效果里有坏处的事件，所有选项都不算)。例外：选项能得到继承人或统治者
     (define_heir/define_ruler)这类重大好处的，即使有代价也算可选。例外：选项能得到继承人或统治者
     (define_heir/define_ruler)这类重大好处的，即使有代价也算可选。不写死事件清单，换 mod 也自动跟着变。
   - 运行时随机池检查玩家的事件时，用引擎自己的选项条件判断玩家当下能选哪些选项：能选的选项里有纯好处的才进
     随机池，否则对玩家判为不满足条件(不进池、不占权重)。AI 不受影响。
   - 会清除标记、结束事变/灾难等改流程状态的事件、隐藏事件不拦(拦了可能卡住事变等流程)。
7. 相容所有宗教和文化：
   - 宗教：所有宗教对玩家都按本教算(引擎“是否本教”判断 0x36e300)，容忍度一律用本教容忍度，没有异端/异教的
     不满等惩罚；传教士也不会再去改宗(都算本教)。
   - 文化：玩家认可所有文化(“是否认可文化”判断 0x2fc3e0，触发器等用它)，玩家省份不加“非认可文化”
     “同文化组”修正(税收、人力、不满)；文化自身的修正照常。省份界面里玩家省份的文化名也按认可文化显示(绿色)。
8. 成就(对所有人生效)：装了 mod、改过的游戏/存档、非铁人模式也能获得成就。
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
import json
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
        self.path = Path(path)
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
    # 事件能否触发(rcx 事件, rdx 作用域, 返回 al)：随机事件池 0x9426d0 用它逐个筛候选；事件 +0x7E0 标题键
    "EVPOSSIBLE": (
        "48 89 5C 24 08 48 89 6C 24 10 48 89 74 24 18 48 89 7C 24 20 41 54 41 56 41 57 48 83 EC 20 80 B9 "
        "64 0A 00 00 00 4C 8B E2 48 8B D9 74 ?? 80 3D ?? ?? ?? ?? 00 74 ?? 80 3D ?? ?? ?? ?? 00 74 ??"
    ),
    # 某宗教对国家算不算本教(rcx 国家, rdx 宗教, r8 第二宗教)：国教、次要宗教、融合/调和宗教；容忍度 0x3003b0 先问它
    "RELOWN": (
        "48 89 5C 24 08 48 89 6C 24 10 48 89 74 24 18 57 41 56 41 57 48 83 EC 20 48 8B 99 38 0F 00 00 48 "
        "8B E9 4C 8B B1 30 0F 00 00 4D 8B F8 48 8B CB 48 8B FA 48 8B 03 FF 50 40 84 C0 74 ?? 48 3B DF"
    ),
    # 某文化是不是国家的认可文化(rcx 国家, rdx 文化)：文化组联合、主文化(+0x1228)、认可文化链表(+0x1230)
    "CULTACC": (
        "48 89 5C 24 08 57 48 83 EC 20 48 8B DA 48 8B F9 E8 ?? ?? ?? ?? 48 39 83 88 00 00 00 74 ?? 48 3B "
        "9F 28 12 00 00 75 ?? B0 01 48 8B 5C 24 30 48 83 C4 20 5F C3 48 8B 87 30 12 00 00 48 85 C0 74 ??"
    ),
    # 省份文化修正(rdi 上下文：+0x18 所有者，+0x20 省份)：+0x4E 处判断认可文化，不认可时加同文化组/非认可文化修正
    "CULTMOD": (
        "48 89 5C 24 10 48 89 6C 24 18 48 89 74 24 20 57 48 83 EC 20 48 8B 41 20 48 8B F9 48 8B 31 48 83 "
        "C6 10 48 8B CE 48 8B 98 40 04 00 00 48 8D 93 58 02 00 00 E8 ?? ?? ?? ?? 48 8B 93 88 00 00 00 48 "
        "8B CE 48 81 C2 70 02 00 00 E8 ?? ?? ?? ?? 48 8B 6F 18 48 3B 9D 28 12 00 00 0F 84 ?? ?? ?? ??"
    ),
    # 省份界面刷新(r14 界面，+0x20 省份)：文化名颜色，sil=认可(绿)，同文化组黄，其他红；+0x110 处 mov eax,0x47a7(§G)
    "CULTUI": (
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8B EC 48 81 EC 80 00 "
        "00 00 4C 8B F1 45 33 ED 44 89 6D 30 4C 39 69 18 0F 84 ?? ?? ?? ?? 48 8B 59 20 48 85 DB 0F 84 ?? "
        "?? ?? ?? 48 8B 15 ?? ?? ?? ?? 48 8B 83 68 04 00 00 48 C1 E8 20 48 0F BF C8 48 8B 82 18 01 00 00 "
        "48 8B 14 C8 4C 8B BA 28 12 00 00 48 8B 9B 40 04 00 00 49 3B DF 75 ?? 40 B6 01 EB ?? 48 8B 82 30 "
        "12 00 00 48 85 C0 74 ?? 48 39 18 74 ?? 48 8B 48 10 48 8B C1 48 85 C9 75 ?? 48 85 C0 40 0F 95 C6 "
        "BF 01 00 00 00 4D 85 FF 74 ?? 48 85 DB 74 ?? 48 8B 83 88 00 00 00 49 39 87 88 00 00 00 75 ?? 44 "
        "0F B6 E7 EB ?? 45 32 E4 48 85 DB 74 ?? 48 8B CA E8 ?? ?? ?? ?? 49 8B 4E 20 48 8B 91 40 04 00 00 "
        "40 0F B6 F6 48 3B 82 88 00 00 00 0F 44 F7 4C 89 6D A0 BF 0F 00 00 00 48 89 7D B8 48 C7 45 B0 02 "
        "00 00 00 B8 A7 52 00 00 66 89 45 A0 44 88 6D A2 B8 A7 47 00 00 40 84 F6 75 ?? 4C 3B FB 74 ?? 45 "
        "84 E4 74 ?? B8 A7 59 00 00"
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
    "EVPOOL": (
        "0000000000000000000000000000000053565755415441554156415751524883ec284989cc4989d5e8d3010000"
        "4983faff0f84b50100004d85ed0f84ac010000498d452c41807d4b007404498d4544488b104889d148c1e93884"
        "c97535496345284885c00f88820100004869c0102e0000488b0d88ffffff488b094885c90f8468010000480381"
        "a81c0000488b9068040000e8bf0100000f854f0100004d85e40f84460100004d8d8c24e00700004d8b59104d85"
        "db0f84310100004981fb000100000f8724010000498379181072034d8b0948b825232284e49cf2cb48b9b30100"
        "0000010000410fb6114831d0480fafc149ffc149ffcb75ed4c8b0d08ffffff4d85c90f84e30000004d8b194531"
        "d24d39da0f83d40000004b8d0c1a48d1e9488d1449498b54d1084839c2740d72054989cbebdb4c8d5101ebd548"
        "8d0449498d44c1084c8b7808488b5810498bb42490070000498bbc24980700004829f748c1ff034883ff400f87"
        "800000004531f64531e44939fe735b4a8b04f64885c0744d4c0fa3f3733b486388bc000000488ba8b000000048"
        "8d4ccd0048894c2420483b6c2420731c488b4d004885c9740d4c89ea488b01ff503084c074124883c508ebdd4d"
        "0fa3f7722541bc0100000049ffc6eba04585e474154883c4285a59415f415e415d415c5d5f5e5b31c0c34883c4"
        "285a59415f415e415d415c5d5f5e5beb6e49c7c2ffffffff488b05f2fdffff488b004885c0743f488b90601e00"
        "0080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080"
        "f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "RETTRUE": (
        "00000000000000005152415250e82d0000004983faff7420488b4c24184885c97416488b5120e86a000000750b"
        "58415a5a59b801000000c358415a5a59eb6e49c7c2ffffffff488b05b3ffffff488b004885c0743f488b90601e"
        "000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e930"
        "80f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "CULTSKIP": (
        "000000000000000000000000000000005152415250e8300000004983faff7423488b4f184885c9741a488b5120"
        "e86e000000750f58415a5a59488b6f18ff25c5ffffff58415a5a59eb6e49c7c2ffffffff488b05a8ffffff488b"
        "004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e938"
        "84c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c3"
        "80c901c3"
    ),
    "CULTCOLOR": (
        "00000000000000005152415250e8270000004983faff741a498b4e204885c97411488b9168040000e862000000"
        "750340b60158415a5a59eb6e49c7c2ffffffff488b05b9ffffff488b004885c0743f488b90601e000080b8661e"
        "000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848"
        "c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
}

# (名称, 特征码键, 挂钩处相对命中处的偏移, 被搬走的原指令长度, 机器码键, 数据区各项)
# 数据区：glob = 游戏对象全局指针，dside = 战斗骰子面数(COMBAT_DICE_SIDE)，wipe = 原版全歼函数，
#         direct = 召集循环里“盟友直接加入战争”的分支，
#         @badev = 随机事件表(安装时分析游戏和 mod 的脚本生成，写在补丁内存里)，
#         cultskip = 省份文化修正里“主文化，不加惩罚”的出口
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
    ("玩家的随机池只留有纯好处的事件", "EVPOSSIBLE", 0, 5, "EVPOOL", ("glob", "@badev")),
    ("所有宗教都按本教算", "RELOWN", 0, 5, "RETTRUE", ("glob",)),
    ("认可所有文化", "CULTACC", 0, 5, "RETTRUE", ("glob",)),
    ("省份没有非认可文化/同文化组惩罚", "CULTMOD", 0x4E, 11, "CULTSKIP", ("glob", "cultskip")),
    ("省份界面文化名显示为认可(绿色)", "CULTUI", 0x110, 5, "CULTCOLOR", ("glob",)),
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


def fnv1a64(s):
    h = 0xCBF29CE484222325
    for c in s.encode("utf-8"):
        h = ((h ^ c) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


# ---------------------------------------------------------------------------
# 恶性随机事件分析(安装时现算，不写死清单)
# ---------------------------------------------------------------------------
# 读游戏目录 + dlc_load.json 里启用的 mod 的脚本(后加载的同名文件覆盖前面的)，找出随机触发的事件
# (on_actions 的 random_events、各 pulse 的 events、平均触发时间)，对每个选项判断是否“只有坏处”。
# 结果按事件标题键做成表：每个事件一项 {标题键哈希, 纯好处的选项位图, 要在运行时检查可用性的选项位图}；
# 运行时只看玩家当下能选的选项：其中有纯好处的(有好处且没有任何坏处)才进玩家的随机池，否则不进。

_PDX_TOK = re.compile(r'\s+|#[^\n]*|"(?:[^"\\]|\\.)*"|[{}]|[<>!]?=|[<>]|[^\s{}=<>#"]+', re.S)
_PDX_OPS = ('=', '<', '>', '<=', '>=', '!=', '==')


def pdx_parse(s):
    toks = [t for t in (m.group() for m in _PDX_TOK.finditer(s)) if not (t[0].isspace() or t[0] == '#')]
    i = 0

    def block():
        nonlocal i
        out = []
        while i < len(toks):
            t = toks[i]
            if t == '}':
                i += 1
                return out
            if i + 1 < len(toks) and toks[i + 1] in _PDX_OPS:
                op = toks[i + 1]
                i += 2
                if i < len(toks) and toks[i] == '{':
                    i += 1
                    v = block()
                else:
                    v = toks[i].strip('"') if i < len(toks) else ''
                    i += 1
                out.append((t.strip('"'), op, v))
            elif t == '{':
                i += 1
                out.append(('', None, block()))
            else:
                out.append((t.strip('"'), None, None))
                i += 1
        return out
    return block()


def pdx_read(path):
    b = Path(path).read_bytes()
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode('latin-1')


def documents_dir():
    """“我的文档”(可能被重定向)下的 EU4 目录。"""
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return Path(buf.value) / "Paradox Interactive" / "Europa Universalis IV"
    except OSError:
        pass
    return Path.home() / "Documents" / "Paradox Interactive" / "Europa Universalis IV"


def content_roots(game_dir):
    """按加载顺序返回内容目录：游戏目录，然后 dlc_load.json 里启用的 mod。"""
    roots = [Path(game_dir)]
    doc = documents_dir()
    try:
        mods = json.loads((doc / "dlc_load.json").read_text(encoding="utf-8")).get("enabled_mods", [])
    except (OSError, ValueError):
        mods = []
    for m in mods:
        try:
            desc = pdx_parse(pdx_read(doc / m))
        except OSError:
            continue
        for k, _, v in desc:
            if k == "path" and isinstance(v, str):
                p = Path(v)
                roots.append(p if p.is_absolute() else doc / p)
    return roots


def merged_files(roots, sub):
    m = {}
    for r in roots:
        d = r / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix.lower() == ".txt":
                    m[f.name] = f
    return [m[k] for k in sorted(m)]


# 修正：值越大越坏的(其余默认越大越好)
_MOD_BAD_UP = {
    'global_unrest', 'local_unrest', 'war_exhaustion', 'global_autonomy', 'local_autonomy', 'min_autonomy',
    'min_local_autonomy', 'interest', 'liberty_desire', 'ae_impact', 'aggressive_expansion_impact', 'land_attrition',
    'naval_attrition', 'inflation', 'stability_cost_modifier', 'overextension_impact_modifier', 'unjustified_demands',
    'monthly_war_exhaustion', 'yearly_corruption', 'yearly_absolutism_decrease', 'devastation', 'local_devastation',
    'prestige_decay', 'harsh_treatment_cost', 'years_of_nationalism', 'local_years_of_nationalism',
    'rebel_support_efficiency', 'local_rebel_support_efficiency', 'fire_damage_received', 'shock_damage_received',
    'global_monthly_devastation', 'local_monthly_devastation', 'yearly_karma_decay', 'enemy_core_creation',
}
# 效果：值为正时是坏处的
_EFF_POS_BAD = ('corruption', 'war_exhaustion', 'inflation', 'autonomy', 'devastation', 'nationalism', 'unrest',
                'doom', 'liberty_desire', 'aggressive_expansion', 'overextension', 'heretic', 'rebel', 'karma')
_EFF_ALWAYS_BAD = {'kill_heir', 'kill_advisor', 'remove_advisor', 'kill_consort', 'kill_units', 'remove_core',
                   'remove_claim', 'cede_province', 'add_loan', 'remove_estate_privilege', 'kill_leader', 'remove_heir',
                   'spawn_rebels'}
_EFF_ALWAYS_GOOD = {'add_core', 'add_claim', 'add_permanent_claim', 'add_territorial_core', 'create_general',
                    'create_admiral', 'create_explorer', 'create_conquistador', 'define_advisor', 'add_building',
                    'add_great_project_tier', 'hire_advisor', 'add_cb', 'add_casus_belli', 'add_idea', 'add_heir',
                    'define_heir', 'define_ruler', 'add_estate_privilege', 'set_estate_privilege', 'add_institution'}
# 重大好处：选项里有这些，即使有代价也算可选
_MAJOR_GOOD = ('define_heir', 'define_ruler')
# 重大好处：选项里有这些，即使有代价也算可选
_MAJOR_GOOD = ('define_heir', 'define_ruler')
_EFF_MODIFIER = {'add_country_modifier', 'add_province_modifier', 'add_permanent_province_modifier',
                 'add_ruler_modifier', 'add_trade_modifier'}
_EFF_SKIP = {'limit', 'trigger', 'ai_chance', 'name', 'tooltip', 'custom_tooltip', 'highlight', 'goto', 'modifier',
             'factor', 'chance', 'duration', 'who', 'type', 'desc', 'text', 'title', 'picture', 'id', 'days',
             'months', 'years', 'key', 'hidden'}
_STATE = re.compile(r'^(clr_\w*flag|end_incident|end_disaster|set_variable|change_variable|subtract_variable|'
                    r'start_incident|start_disaster)$')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mod_sign(name):
    n = name.lower()
    if n in _MOD_BAD_UP or n.endswith('_cost') or 'cost' in n.split('_') or n.endswith('_decay') \
            or 'unrest' in n or 'rebel' in n or ('attrition' in n and 'hostile' not in n):
        return -1
    return 1


class EventAnalyzer:
    def __init__(self, roots):
        self.roots = roots
        self.modifiers, self.scripted = {}, {}
        for sub, dst in (('common/event_modifiers', self.modifiers), ('common/static_modifiers', self.modifiers),
                         ('common/scripted_effects', self.scripted)):
            for f in merged_files(roots, sub):
                for k, _, v in pdx_parse(pdx_read(f)):
                    if isinstance(v, list):
                        dst[k] = v

    def modifier_score(self, name):
        s = 0
        for k, _, v in self.modifiers.get(name, []):
            x = _num(v)
            if x:
                s += _mod_sign(k) * (1 if x > 0 else -1)
        return (s > 0) - (s < 0)

    def score(self, block, depth=0):
        """(好处项数, 坏处项数)。封装的脚本效果展开后再算。"""
        g = b = 0
        if depth > 12:
            return 0, 0
        for k, _, v in block:
            if not k or k in _EFF_SKIP:
                continue
            if k in self.scripted:
                x, y = self.score(self.scripted[k], depth + 1)
                g, b = g + x, b + y
            elif k in _EFF_ALWAYS_BAD or k.endswith('_rebels'):
                b += 1
            elif k in _EFF_ALWAYS_GOOD:
                g += 1
            elif k in _EFF_MODIFIER and isinstance(v, list):
                d = {kk: vv for kk, _, vv in v}
                s = self.modifier_score(d.get('name')) if d.get('name') else 0
                if not s and _num(d.get('power')):  # add_trade_modifier { power = -10 }
                    s = 1 if _num(d.get('power')) > 0 else -1
                g, b = g + (s > 0), b + (s < 0)
            elif (k.startswith('add_') or k.startswith('change_')) and (
                    _num(v) or (isinstance(v, list) and any(_num(vv) for kk, _, vv in v
                                                            if kk in ('value', 'amount', 'loyalty', 'influence')))):
                x = _num(v) if _num(v) else next(_num(vv) for kk, _, vv in v
                                                  if kk in ('value', 'amount', 'loyalty', 'influence') and _num(vv))
                if (x > 0) != any(w in k for w in _EFF_POS_BAD):
                    g += 1
                else:
                    b += 1
            elif isinstance(v, list):
                x, y = self.score(v, depth + 1)
                g, b = g + x, b + y
        return g, b

    @staticmethod
    def _touches_state(block):
        for k, _, v in block:
            if _STATE.match(k or '') or (isinstance(v, list) and EventAnalyzer._touches_state(v)):
                return True
        return False

    @staticmethod
    def _has_key(block, keys):
        for k, _, v in block:
            if k in keys or (isinstance(v, list) and EventAnalyzer._has_key(v, keys)):
                return True
        return False

    @staticmethod
    def _mentions(block, word):
        for k, _, v in block:
            if word in (k or '') or (isinstance(v, str) and word in v) or \
                    (isinstance(v, list) and EventAnalyzer._mentions(v, word)):
                return True
        return False

    def analyze(self):
        """返回 {标题键: (纯好处的选项位图, 运行时检查可用性的选项位图)}，只含可能被拦的事件。"""
        events = {}
        for f in merged_files(self.roots, 'events'):
            for k, _, v in pdx_parse(pdx_read(f)):
                if k in ('country_event', 'province_event') and isinstance(v, list):
                    d = {}
                    for kk, _, vv in v:
                        d.setdefault(kk, []).append(vv)
                    if d.get('id'):
                        events[d['id'][0]] = d
        rnd = set()
        for f in merged_files(self.roots, 'common/on_actions'):
            for k, _, v in pdx_parse(pdx_read(f)):
                if not isinstance(v, list):
                    continue
                for kk, _, vv in v:
                    if kk == 'random_events' and isinstance(vv, list):
                        rnd |= {e for _, _, e in vv if isinstance(e, str) and e != '0'}
                    elif kk == 'events' and 'pulse' in k and isinstance(vv, list):
                        rnd |= {e for e, _, _ in vv if e}
        for i, d in events.items():
            if 'mean_time_to_happen' in d and d.get('is_triggered_only', ['no'])[0] != 'yes':
                rnd.add(i)
        by_title = {}
        for i, d in events.items():
            t = d.get('title', [None])[0]
            if isinstance(t, str):
                by_title.setdefault(t, []).append(i)
        out = {}
        for i in rnd:
            d = events.get(i)
            t = d.get('title', [None])[0] if d else None
            if not isinstance(t, str) or t in out:
                continue
            # 按标题键识别：共用这个标题的事件(包括非随机的)判定结果都一样才能拦，否则区分不开就不拦
            masks = {self._masks(events[j]) for j in by_title[t]}
            m = masks.pop() if len(masks) == 1 else None
            out[t] = m if m and m[0] != m[2] else None
        return {t: (g, c) for t, v in out.items() if v for g, c, _ in [v]}

    def _masks(self, d):
        """(纯好处的选项位图, 检查可用性的选项位图, 全部选项位图)；不该拦的事件返回 None。"""
        if d.get('hidden', ['no'])[0] == 'yes':
            return None
        opts = [o for o in d.get('option', []) if isinstance(o, list)]
        if not opts or len(opts) > 64:
            return None
        if any(isinstance(v, list) and self._touches_state(v)
               for key in ('option', 'immediate', 'after') for v in d.get(key, [])):
            return None
        imm_bad = any(isinstance(v, list) and self.score(v)[1] for v in d.get('immediate', []))
        good = check = 0
        for n, o in enumerate(opts):
            g, b = self.score(o)
            if g and not b and not imm_bad and not self._has_key(o, ('country_event', 'province_event')):
                good |= 1 << n  # 纯好处：有好处、没有坏处，也不接着触发别的事件(后续好坏未知)
            elif not imm_bad and self._has_key(o, _MAJOR_GOOD):
                good |= 1 << n  # 重大好处(得到继承人/统治者)：有代价也算可选
            elif not imm_bad and self._has_key(o, _MAJOR_GOOD):
                good |= 1 << n  # 重大好处(得到继承人/统治者)：有代价也算可选
            trig = [v for k, _, v in o if k == 'trigger' and isinstance(v, list)]
            if trig and not any(self._mentions(tb, 'event_target') for tb in trig):
                check |= 1 << n  # 有选项条件：运行时问引擎能不能选(用到事件目标的当作能选)
        return good, check, (1 << len(opts)) - 1


def blob_badev(game_dir):
    """随机事件表：数量 + 按哈希升序的 {标题键哈希, 纯好处的选项位图, 检查可用性的选项位图}。"""
    res = EventAnalyzer(content_roots(game_dir)).analyze()
    ent = sorted((fnv1a64(t), g, c) for t, (g, c) in res.items())
    log(f"随机事件分析：{len(ent)} 个随机事件不一定有纯好处的选项(运行时看玩家能选的选项决定进不进随机池)")
    return struct.pack("<Q", len(ent)) + b"".join(struct.pack("<QQQ", *e) for e in ent)


# 补丁内存里的数据块：键 -> 函数(img)，安装时生成
BLOBS = {"@badev": lambda img: blob_badev(img.path.parent)}

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
    # 省份文化修正里“主文化，不加惩罚”的出口：挂钩处 +0xB 的 je(0F 84 rel32)目标
    e = R.get("entry:省份没有非认可文化/同文化组惩罚")
    if e is not None and img.bytes_at(e + 0xB, 2) == b"\x0f\x84":
        R["cultskip"] = e + 0xB + 6 + struct.unpack("<i", img.bytes_at(e + 0xD, 4))[0]
    elif e is not None:
        problems.append("省份文化修正的出口: 找不到")
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
    arena = P.alloc_near(h, base + img.text_rva + len(img.text) // 2, 0x20000)
    if not arena:
        return out + [("补丁", "分配内存失败")]
    cur_off = 0
    blob_at = {}
    for k in {k for *_, keys in todo for k in keys if k.startswith("@")}:
        data = BLOBS[k](img)
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
