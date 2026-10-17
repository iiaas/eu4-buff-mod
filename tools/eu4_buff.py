#!/usr/bin/env python3
"""EU4 引擎内补丁工具(纯 Python，不需要 mod 脚本；Steam 启动时起一个守候进程，游戏每次启动都自动装补丁，脚本更新后自动换新版)。

原理
----
在 eu4.exe 进程里给几处引擎代码挂上小段机器码，先判断是否涉及玩家国家，不涉及就照常执行原代码，
所以 AI 之间的战斗、围城不受影响。游戏每次启动都要重装(Steam 启动项会自动装)。

功能(只对玩家国家，成就除外)
----
一、战斗
1. 全歼(已关闭)：玩家一方打赢陆战，败方部队直接全军覆没(走原版全歼流程，原版的兵力返还等规则不变)。
   海战不处理：原版的全歼只针对陆军。
2. 骰子：
   - 陆战、海战每次掷骰：玩家一方固定最大(骰子面数 - 1，默认 9)，敌方固定 0。
   - 强攻要塞每次掷骰：同上。
   - 围城每次掷骰：玩家攻城固定 14(最大)；敌人围攻玩家控制的要塞固定 1(最小，原版规则下 1 可能被算成 2)。
   - 指挥官、地形、渡河等加减值照原版计算。
3. 无损耗：玩家的陆军部队损耗为 0(含行军、围城、海运时的损耗)；玩家的舰队损耗也为 0(出海时间、缺水手、敌对海域等)。
4. 将领：玩家生成将领/海军将领(征召、事件给的等)时，能力点总数直接取当前能得到的最大值(原版是在最小和最大
   之间随机)，之后照原版随机分到火力/冲击/机动/围城，每项不超过上限。

二、外交和战争
5. 宣战无惩罚：玩家宣战时稳定度等惩罚为 0(无理由宣战、撕毁休战协议、联姻、良好关系等各项)，
   宣战也不增加侵略扩张。
6. 盟友必定参战(已关闭)：
   - 玩家宣战或被宣战时，所有盟友像附庸一样直接加入战争，不需要对方同意、不消耗人情；
     宣战窗口里盟友和附庸一样显示为“强制参战”(不能取消)，并计入兵力对比。已被选作敌方共同交战国的除外。
   - 战争中用“召集参战”召集盟友仍照原版(要对方同意、消耗人情)。
7. 属国按帝国诸侯算：神圣罗马帝国撤销上诉特权后，皇帝的帝国诸侯属国不占外交关系、相对实力(独立倾向)
   只和自己比；玩家的属国走原版这两处同样的分支：
   - 外交关系名额(0xa752b0)：宗主是玩家的属国关系不占名额(属国类型本来就不占的照旧)。
   - 独立倾向里的相对实力(0x3b22d0)：玩家属国的 relative_power_class 改为 1，只拿它自己(和支持它独立的国家)
     跟玩家比，不再把同类属国加在一起；本来不算相对实力的(朝贡国、殖民地等，类 0)照旧。

三、事件
8. 坏事件：判断事件好坏时，对玩家当下能选的每个选项(用游戏自己的选项条件判断)，用游戏生成事件窗口里选项
   提示的函数生成效果说明：有绿色、没有红色的是好选项；另外能给继承人(define_heir)的选项也算好选项。
   没有颜色、只有红色、红绿都有的都不算好。能选的选项必须全是好选项，有一个不是就是坏事件。
   - 随机池(on_actions 的 random_events)：照原版抽，抽中发给玩家的坏事件，就在同一个池子当时符合条件的好事件里
     按权重重抽一个，“出不出事件”的概率和原版一样。池里一个好事件都没有时这次什么都不发生。
   - 国家/省份平均触发时间(mean_time_to_happen)的事件：照原版掷骰，掷中的是发给玩家的坏事件就不发生(不替换)。
   - 被别的事件/决议直接调用的连锁事件、延迟事件、on_action 固定事件不动。AI 不受影响。
9. 国家专属事件不拦截：触发条件里有 tag = X(顶层，或在 OR/AND 里)的事件一律不算坏事件，照原版发生。
10. 平均触发时间最多 5 年：玩家的平均触发时间事件，算上修正后超过 5 年的按 5 年算，短于 5 年的照原版。

四、国家
11. 相容所有宗教和文化：
   - 宗教：所有宗教对玩家都按本教算(引擎“是否本教”判断 0x36e300)，容忍度一律用本教容忍度，没有异端/异教的
     不满等惩罚；传教士也不会再去改宗(都算本教)。
   - 国教：玩家有的宗教(有省份是这个宗教)都按国教算：脚本触发器 religion = X(国家作用域)在 X 是国教或玩家有
     X 宗教的省份时为真；religion_group = G(国家作用域)在国教属于 G 或玩家有宗教属于 G 的省份时为真；
     has_owner_religion 对玩家的省份按“是主人国教”算(= no 时为假)。只在决议的条件里生效(见下)。
   - 融合宗教(同腾格里)：脚本触发器 secondary_religion / secondary_religion_group 对玩家一律为真，
     相当于所有宗教都是玩家的融合宗教；伟大工程(如天房)等“国教或融合宗教”的条件因此都满足。
   - 文化(莫卧儿底万)：玩家不论什么政体，引擎都按有莫卧儿政体(mughal_government)的同化文化表处理：
     玩家拥有某个文化的任意一个省份(新大陆的省份也算，原版莫卧儿不算)，这个文化就同化为认可文化(每月检查；
     原版莫卧儿要拥有全部省份)，之后失去
     省份也不会取消；拥有某文化组的任意一个省份就得到这个组的同化加成(莫卧儿政体里写的各组修正，一个都没有
     时取消)。文化界面显示同化文化/同化加成，和莫卧儿一样。
     玩家原有的文化组联合(帝国等级时同组文化都认可)照常保留。没有“同化”的文化仍按原版算非认可文化。
   - 主文化：玩家有的文化(有省份是这个文化)都按主文化算：脚本触发器 primary_culture = X(国家作用域)在 X 是主文化
     或玩家有 X 文化的省份时为真；culture_group = G(国家作用域)在主文化属于 G 或玩家有文化属于 G 的省份时为真；
     has_owner_culture 对玩家的省份按“是主人主文化”算(= no 时为假)。
   - 科技：脚本触发器 adm_tech / dip_tech / mil_tech 对玩家一律为真(所有地方：决议、事件、任务等；
     如成立德国要求的行政科技 18)。
   - 上面国教、主文化这两项只在决议的条件里生效(决议界面、可用决议提示、执行决议)，用来成立国家等；
     君主称号、事件(选项、触发条件)、任务等其他地方照原版只看国教和主文化，称号和事件里的名字都正常。
12. 特质：玩家的统治者、继承人、配偶随机获得特质时只出好特质(按原版权重在好特质里挑)。好坏按颜色：特质的
   每一项修正在界面上都是绿色(游戏修正定义里“正值是好处”的标志和数值正负一致)才算好特质；有红色的不出。
   能选的特质里一个好的都没有时照原版挑。事件/决议直接指定的特质不受影响。
13. 三维：玩家国家生成统治者、继承人、配偶时，每一维都加 2(随机出来的和事件/决议用脚本指定的都算)，
   不超过这一维的上限(一般 6)；本来就到上限的不变。每个人物每一维只加一次(生成函数对同一人物会走几遍)：
   随机生成的在生成时加，脚本指定的只在事件/决议效果那一遍加。之后再用 change_adm 等改动现有人物的不加。
14. 贸易：任何贸易节点，玩家的贸易力量占比超过 50%(按游戏同样的公式：力量 x 系数，封顶，加转入减转出)，
   每次结算时其他国家在这个节点的转入转出清零、贸易力量封顶到 1(倍率和原始力量都封顶到 1)；
   其他国家仍在节点里有力量，商人不会因此被召回。占比每次结算按原值重新判断。

15. 殖民地商品：玩家和玩家属国(殖民领等，宗主是玩家)的省份(商品原为“未知”)被殖民、有了主人确定商品时，不再按权重随机，而是在这个省份
   可能出现(权重大于 0)的商品里取当前价格最高的；黄金(价格表里是 0，收入按产量直接入国库)按最高算，
   可能出黄金就一定是黄金。商品本来就确定的省份不受影响。

五、成就
16. 成就(对所有人生效)：装了 mod、改过的游戏/存档、非铁人模式也能获得成就。
   - 脚本触发器 ironman 恒按“是铁人”判断(成就条件里都写了 ironman = yes)，
     所以其他脚本里的 ironman 判断也会受影响。
   - 难度、AI 难度、幸运国家、开局设置、多人游戏、指定国家等其他成就条件不变。

脚本条件(trigger)修改一览
------------------------
作用域是玩家国家(或玩家的省份)时：
  始终为真
    secondary_religion = X / secondary_religion_group = G     所有宗教都是融合宗教(同腾格里)
    adm_tech / dip_tech / mil_tech = N                        科技等级(所有地方)
    ironman = yes                                             对所有人(成就)
  玩家有就为真(拥有该宗教/文化的省份，或本来就是国教/主文化)；只在决议的条件里，其他地方照原版
    religion = X / religion_group = G                         国家作用域
    primary_culture = X / culture_group = G                   国家作用域
  玩家的省份为真(写 = no 时为假)；只在决议的条件里，其他地方照原版
    has_owner_religion / has_owner_culture
  另外引擎“某宗教是否本教”对玩家恒为是(宽容度、异端不满、传教士用它，不是脚本条件)。
  accepted_culture 等认可文化判断照原版(同化后的文化会变成认可文化)。
  省份作用域的 religion / culture 等(看省份自己)和 AI 一律照原版。

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
import traceback
from ctypes import wintypes
from pathlib import Path

SELF = Path(__file__).resolve()
HERE = SELF.parent
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
    def wait_exit(h, ms=0xFFFFFFFF):
        """等进程(h)退出，最多 ms 毫秒。退出了返回 True(并关闭句柄)，超时返回 False。"""
        P.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        P.k32.WaitForSingleObject.restype = wintypes.DWORD
        if P.k32.WaitForSingleObject(h, ms) == 0x102:  # WAIT_TIMEOUT
            return False
        P.k32.CloseHandle(h)
        return True

    @staticmethod
    def single_instance(name):
        """建一个命名互斥量；已有同名的(另一个守候进程在运行)返回 None。返回的句柄要一直留着。"""
        P.k32.CreateMutexW.restype = wintypes.HANDLE
        P.k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = P.k32.CreateMutexW(None, False, name)
        if h and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：关掉这个句柄，免得一直占着互斥量
            P.k32.CloseHandle(h)
            return None
        return h or None

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
    # 原版消灭一方全部部队 0x21ef40(rcx 一方，部队链表 +0xE8)，全歼函数在结束战斗(0x239ff0)之后调用它
    "DESTROYSIDE": (
        "88 54 24 10 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 E1 48 81 EC A8 00 00 00 44 0F B6 E2 "
        "4C 8B E9 0F 57 C0 0F 11 45 C7 48 C7 45 D7 04 00 00 00 48 8D 05 ?? ?? ?? ?? 48 89 45 C7 48 8D 45 "
        "DF 48 89 45 CF 48 8D 05 ?? ?? ?? ?? 48 89 45 A7"
    ),
    # 部队损耗计算(rcx 部队, rdx 结果指针)
    "ATTRITION": (
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8D 6C 24 D9 48 81 EC "
        "A0 00 00 00 4D 8B E8 4C 8B E2 4C 8B F1 33 C9 89 4D 57 49 8B 86 18 02 00 00 48 85 C0 74 ?? 48 8B "
        "80 E8 00 00 00 4C 8B 40 28 41 0F B6 80 28 02 00 00 A8 08 75 ?? 84 C0 8B 1D ?? ?? ?? ?? 79 ?? 8B "
        "1D ?? ?? ?? ?? 48 89 4C 24 20 45 33 C9 41 B8 D7 01 00 00 48 8D 55 57 49 8B CE E8 ?? ?? ?? ?? 8B "
        "00 05 E8 03 00 00 48 63 C8 48 63 C3 48 0F AF C8"
    ),
    # 舰队损耗计算 0x90b2a0(海军虚表第 60 项；rcx 舰队，+0xD0 所属国标签；rdx 结果指针，r9 说明文字)，结果写到 [rdx]
    "NAVALATTR": (
        "4C 89 4C 24 20 4C 89 44 24 18 48 89 54 24 10 55 53 56 57 41 54 41 55 41 56 41 57 48 8D AC 24 38 "
        "FF FF FF 48 81 EC C8 01 00 00 4D 8B F9 48 8B FA 4C 8B E9 45 33 F6 41 8B F6 44 89 B5 10 01 00 00"
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
    # 贸易节点结算 0xb52160 逐国累加前(+0x3B 挂钩，[rbp+0x120] 节点，条目 +0x18 个数 +0x24 每项 0xC0)
    "TRADE50SITE": (
        "4C 8B BD 20 01 00 00 48 8B BD 38 01 00 00 4C 8B 45 80 48 BB CF F7 53 E3 A5 9B C4 20 49 C7 C2 FF "
        "FF FF FF 48 83 C7 08 48 89 BD 38 01 00 00 49 3B F8 0F 85 ?? ?? ?? ?? 4C 8B 6D A8 4D 8B B5 08 1D "
        "00 00 49 63 85 14 1D 00 00 4D 8D 24 C6 44 8B 05 ?? ?? ?? ?? 4D 3B F4"
    ),
    # 殖民地确定商品 0xa03dd0(rbx 省份)：+0x98 按权重随机抽([rsp+0x30] 候选 {商品, 权重} 每项 0x10)，+0xDB 设置商品
    "GOODSROLL": (
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 50 0F B6 F2 48 8B D9 48 8B 89 58 04 00 00 48 85 C9 74 "
        "?? 48 8B 01 FF 50 40 84 C0 74 ?? 48 8B 8B 58 04 00 00 E8 ?? ?? ?? ?? 84 C0 0F 84 ?? ?? ?? ?? 48 "
        "8B 83 68 04 00 00 48 C1 E8 38 84 C0 0F 84 ?? ?? ?? ??"
    ),
    # 省份定期更新 0x9f35c0 里殖民地成长时确定商品(r13 省份，[rsp+0x40] 候选)：+0x54 按权重随机抽，+0x9B 设置商品(商品在 rdx)
    "GOODSGROW": (
        "49 8B CD E8 ?? ?? ?? ?? 84 C0 0F 84 ?? ?? ?? ?? 0F 57 C0 F3 0F 7F 44 24 40 4C 89 7C 24 50 44 89 "
        "7C 24 58 C6 44 24 20 00 41 B1 01 4D 8B 85 68 04 00 00 48 8D 54 24 40 49 8B CD E8 ?? ?? ?? ?? 48 "
        "8B 44 24 48 48 8B 54 24 40 48 2B C2 48 C1 F8 04 85 C0 7E ?? BA 8E 15 00 00 48 8D 0D ?? ?? ?? ??"
    ),
    # 生成将领 0x863420 里取能力点总数处(+0x29：lea r10d,[rdx+r8] 下限+随机；[rbp+0x40] 上限，r15 = 国家 +0x28D0)
    "LEADERGEN": (
        "BA 43 03 00 00 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 44 8B 45 50 99 8B 4D 40 41 BD 04 00 00 00 41 "
        "2B C8 45 8B CD FF C1 F7 F9 46 8D 14 02 4C 8B C3 41 8B F2 41 83 FA 0A 7E ?? 41 8B 10 41 8D 4A F5 "
        "C1 E9 02 FF C1 8B C1 F7 D8 41 8D 34 82 8D 04 11 41 89 00"
    ),
    # 外交关系占不占名额 0xa752b0(rdi 关系条目：+8 宗主，+0x10 属国)：+0xE 比较宗主是不是皇帝，+0x44 帝国诸侯不占名额的返回
    "SUBJSLOT": (
        "4C 8B 0D ?? ?? ?? ?? 49 8B 89 E0 15 00 00 48 8B 47 08 48 C1 E8 20 48 C1 E9 20 66 3B C1 75 ?? 48 "
        "8B 05 ?? ?? ?? ?? 49 C1 EA 20 49 0F BF CA 48 8B 80 18 01 00 00 48 8B 0C C8 48 8B 81 B0 14 00 00 "
        "F6 80 58 09 00 00 20 74 ?? 41 38 B1 D2 1A 00 00 74 ?? 32 C0 E9 ?? ?? ?? ?? 48 8B 41 08 EB ??"
    ),
    # 属国相对实力 0x3b22d0 里(r9 宗主，rbx 属国，[rbp+0x1A0] relative_power_class)：+0x14 比较宗主是不是皇帝，帝国诸侯改为 1
    "SUBJPOWER": (
        "4C 8B 2D ?? ?? ?? ?? 41 BF E8 03 00 00 48 8B 15 ?? ?? ?? ?? 48 8B 8A E0 15 00 00 48 C1 E9 20 49 "
        "8B 41 20 48 C1 E8 20 66 3B C1 75 ?? 48 8B 83 B0 14 00 00 F6 80 58 09 00 00 20 74 ?? 8B 8D A0 01 "
        "00 00 B8 01 00 00 00 80 BA D2 1A 00 00 00 0F 45 C8 89 8D A0 01 00 00"
    ),
    # 随机特质 0xa4f9f0：候选按权重放进数组后取余挑选处(rbx 数组，r12 个数，[rbp+0x1A8] 随机数，[rbp+0x1A0] 人物)，+0x18 挂钩
    "PERSPICK": (
        "4C 8B AD B8 01 00 00 48 8B 5C 24 50 4C 2B E3 49 C1 FC 03 4D 85 E4 74 ?? 8B 85 A8 01 00 00 99 41 "
        "F7 FC 48 8B 34 D3 45 33 F6 E9 ?? ?? ?? ?? E8 ?? ?? ?? ??"
    ),
    # 生成统治者/继承人/配偶三维 0xa4b6a0(rcx 三维数组，r8 所属国家标签)：+0x47 shr r8,0x20，+0x21C 随机生成一维截断前，+0x24A 每维循环末尾(rsi 这一维，r14d 上限)
    "MONSKILL": (
        "48 89 5C 24 08 48 89 6C 24 18 48 89 74 24 20 48 89 54 24 10 57 41 54 41 55 41 56 41 57 48 83 EC "
        "20 49 8B D9 48 8B F1 44 0F B6 64 24 70 41 83 F4 01 41 FF CC 44 89 64 24 70 48 8B 05 ?? ?? ?? ?? "
        "48 8B 90 18 01 00 00 49 C1 E8 20 49 0F BF C0 48 8B 0C C2"
    ),
    # 事件效果(define_ruler 等，0x6526b0)对临时三维调用生成函数 0xa4b6a0：+0x4B 是 call 之后的地址
    "DEFRULERGEN": (
        "C7 45 E8 E8 03 00 00 80 7F 78 00 0F 95 C1 4D 8B 44 24 20 48 8D 5F 28 48 89 5D 10 48 8D 86 1C 01 "
        "00 00 48 89 44 24 40 48 8D 85 F0 02 00 00 48 89 44 24 38 48 8D 45 E8 48 89 44 24 30 88 4C 24 20 "
        "4D 8B C8 48 8B CB E8 ?? ?? ?? ??"
    ),
    # 事件效果(define_heir/consort 等，0x656db0)对临时三维调用生成函数 0xa4b6a0：+0x4E 是 call 之后的地址
    "DEFHEIRGEN": (
        "C7 85 68 0A 00 00 E8 03 00 00 41 80 7E 78 00 0F 95 C2 4D 8B 45 20 48 8B BD 60 0A 00 00 48 8D 87 "
        "4C 03 00 00 48 89 44 24 40 48 8D 45 40 48 89 44 24 38 48 8D 85 68 0A 00 00 48 89 44 24 30 88 54 "
        "24 20 4D 8B C8 49 8D 4E 28 E8 ?? ?? ?? ??"
    ),
    # 修正登记函数(每种修正：标志、编号、令牌)：+0x19 sub rax,[rip+X] 的 X 是修正定义数组起始指针(每项 0x60，+0x5C 标志)
    "MODREG": (
        "40 55 53 48 8D AC 24 98 FD FF FF 48 81 EC 68 03 00 00 48 8B 05 ?? ?? ?? ?? 48 2B 05 ?? ?? ?? ?? "
        "48 C1 F8 05 48 B9 AB AA AA AA AA AA AA AA"
    ),
    # 某宗教对国家算不算本教(rcx 国家, rdx 宗教, r8 第二宗教)：国教、次要宗教、融合/调和宗教；容忍度 0x3003b0 先问它
    "RELOWN": (
        "48 89 5C 24 08 48 89 6C 24 10 48 89 74 24 18 57 41 56 41 57 48 83 EC 20 48 8B 99 38 0F 00 00 48 "
        "8B E9 4C 8B B1 30 0F 00 00 4D 8B F8 48 8B CB 48 8B FA 48 8B 03 FF 50 40 84 C0 74 ?? 48 3B DF"
    ),
    # 触发器 secondary_religion 的判断函数(rcx 触发器, rdx 作用域)：比较国家 +0xF38(融合宗教)
    "SECREL": (
        "40 53 48 83 EC 20 8B 42 28 48 8B DA 4C 8B 49 68 85 C0 74 ?? 4D 85 C9 75 ?? E8 ?? ?? ?? ?? 4C 8B "
        "C8 8B 43 28 48 63 C8 48 69 D1 10 2E 00 00 48 8B 0D ?? ?? ?? ?? 48 8B 89 A8 1C 00 00 48 8B 8C 0A "
        "68 04 00 00 48 C1 E9 20 4C 0F BF C1"
    ),
    # 触发器 secondary_religion_group 的判断函数(rcx 触发器, rdx 作用域)：比较国家 +0xF38 融合宗教的宗教组
    "SECRELGRP": (
        "40 53 48 83 EC 20 48 83 C1 68 48 8B DA E8 ?? ?? ?? ?? 80 7B 4B 00 BA 2C 00 00 00 B9 44 00 00 00 "
        "0F 44 CA 48 8B 0C 19 48 C1 E9 20 4C 0F BF C1 48 8B 0D ?? ?? ?? ?? 48 8B 91 18 01 00 00 4A 8B 0C "
        "C2 48 8B 91 38 0F 00 00"
    ),
    # 触发器 primary_culture 的判断函数(rcx 触发器, rdx 作用域)：+0x38B 国家作用域比较主文化 [国家+0x1228]
    "PRIMCULT": (
        "48 89 5C 24 08 48 89 74 24 10 48 89 7C 24 18 4C 89 74 24 20 55 48 8D 6C 24 80 48 81 EC 80 01 00 "
        "00 48 8B DA 48 8B F1 BF 44 00 00 00 44 8D 77 E8 83 7A 28 00 0F 85 ?? ?? ?? ?? 8B C7 80 7A 4B 00 "
        "41 0F 44 C6 48 8B 04 10 48 C1 E8 38 84 C0 0F 85 ?? ?? ?? ??"
    ),
    # 触发器 culture_group 的判断函数：+0x63 国家作用域取主文化 [国家+0x1228]，之后比较文化 +0x88(所属组)
    "CULTGRP": (
        "40 53 48 83 EC 20 48 8B DA E8 ?? ?? ?? ?? 4C 8B C8 48 63 43 28 85 C0 74 ?? 48 8B 0D ?? ?? ?? ?? "
        "4C 69 C0 10 2E 00 00 48 8B 91 A8 1C 00 00 49 8B 8C 10 40 04 00 00 EB ??"
    ),
    # 条件工厂里令牌 0x2B4B 起的跳转表分支(含 has_owner_culture 0x2BD7)：+0x14 起始令牌(取负)，+0x32 字节表，+0x39 偏移表
    "OWNSWITCH": (
        "81 FA F7 2C 00 00 0F 8F ?? ?? ?? ?? 0F 84 ?? ?? ?? ?? 81 C2 B5 D4 FF FF 81 FA A6 01 00 00 0F 87 "
        "?? ?? ?? ?? 48 63 C2 48 8D 15 ?? ?? ?? ?? 0F B6 84 02 ?? ?? ?? ?? 8B 8C 82 ?? ?? ?? ?? 48 03 CA "
        "FF E1"
    ),
    # 触发器 religion 的判断函数(+0x68 模式：0 自动/1 国家/2 省份；r8 目标宗教)：+0x95 国家分支比较国教 [国家+0xF30]
    "RELIG": (
        "48 89 5C 24 08 57 48 83 EC 20 48 8B D9 48 8B FA 48 83 C1 70 E8 ?? ?? ?? ?? 8B 53 68 4C 8B C0 B8 "
        "44 00 00 00 44 8D 48 E8 85 D2 75 ?? 39 57 28 75 ?? 38 57 4B 8B C8 41 0F 44 C9 48 8B 0C 39 48 C1 "
        "E9 38 84 C9 75 ?? 83 FA 02 75 ??"
    ),
    # 触发器 religion_group 的判断函数：+0x67 国家分支取国教 [国家+0xF30]，之后比较宗教 +0xD8(所属组)
    "RELGRP": (
        "40 53 48 83 EC 20 48 83 C1 68 48 8B DA E8 ?? ?? ?? ?? 48 63 4B 28 4C 8B C8 85 C9 74 ?? 4C 69 C1 "
        "10 2E 00 00 48 8B 0D ?? ?? ?? ?? 48 8B 91 A8 1C 00 00 49 8B 8C 10 48 04 00 00 EB ??"
    ),
    # 科技触发器(adm_tech/dip_tech/mil_tech，条件工厂按科技类别名生成)判断函数 0xc187c0 结尾：+0x3E 处 cmp esi,edx / setge al(esi 国家科技等级，edx 要求，rdi 作用域)
    "TECHTRIG": (
        "48 8B 85 98 00 00 00 48 C1 E8 20 48 0F BF D0 48 8B 05 ?? ?? ?? ?? 48 8B 88 18 01 00 00 48 8B 04 "
        "D1 48 8B 90 40 24 00 00 48 8B 43 70 8B 48 50 48 8B 82 28 01 00 00 8B 14 88 EB ?? 8B 53 68 3B F2 "
        "0F 9D C0 4C 8D 9C 24 80 01 00 00 49 8B 5B 10 49 8B 73 20 49 8B 7B 28 49 8B E3 5D C3"
    ),
    # 取政体同化文化表 0x7b30d0(rcx 政体 +0x38 改革汇总，[+0x1D0] 同化表变体)：返回 CPdxArray(+8 数据，+0x14 个数)；每月更新、文化界面、同化处理都用它
    "ASSIMGET": (
        "40 53 48 83 EC 60 48 8B 99 D0 01 00 00 48 85 DB 74 ?? 44 8B 03 41 8B C0 C1 E8 1F 41 8B D0 F7 D2 "
        "84 C0 41 0F 44 D0 0F B6 44 24 70 88 44 24 28 E8 ?? ?? ?? ??"
    ),
    # 国家认同的文化组 0x2fc810(文化组联合)：+0x47 call 取同化表，有同化表时不认同文化组
    "ASSIMCG": (
        "48 89 5C 24 08 57 48 83 EC 20 48 8B D9 48 8B B9 20 14 00 00 48 85 FF 74 ?? 48 8B 47 10 EB ?? E8 "
        "?? ?? ?? ?? 90 48 85 FF 74 ?? 48 8D 47 38 EB ?? 48 85 C0 74 ?? 48 83 C0 78 EB ?? E8 ?? ?? ?? ?? "
        "48 83 C0 58 48 8B C8 E8 ?? ?? ?? ?? 83 78 14 00 0F 85 ?? ?? ?? ??"
    ),
    # 同化处理 0x79eb40(rcx 政体，+0x18 国家)：+0x13D 逐文化数省份(r15d 总数，r14d 拥有数，[rbp+0x80] 国家)，+0x707 文化组拥有数 [rbp+0x60] / 总数 [rbp+0x64] 比较
    "ASSIMPROC": (
        "88 54 24 10 48 89 4C 24 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D AC 24 D8 FD FF FF 48 81 EC "
        "28 03 00 00 0F B6 F2 4C 8B E1 48 8B 05 ?? ?? ?? ?? 0F B6 80 84 74 00 00 88 85 80 02 00 00 48 8B "
        "41 18 48 89 85 80 00 00 00 0F 57 C0"
    ),
    # 列出国家省份里的文化 0x2fc600(rcx 国家，+0x1338 拥有省份)：+0x5B 判断 [省份+0x8A0] 虚函数 +0x40(新大陆)为真就跳过，+0x70 是不跳过的路径
    "CULTLIST": (
        "40 57 41 56 41 57 48 83 EC 20 4C 8B B1 38 13 00 00 48 8B FA 48 63 81 44 13 00 00 4D 8D 3C 86 4D "
        "3B F7 0F 84 ?? ?? ?? ?? 48 89 74 24 50 4C 89 64 24 58 49 C7 C4 FF FF FF FF 48 89 5C 24 40 48 89 "
        "6C 24 48 49 63 06 48 69 F0 10 2E 00 00 48 8B 05 ?? ?? ?? ?? 48 03 B0 A8 1C 00 00 48 8B 8E A0 08 "
        "00 00 48 8B 01 FF 50 40 84 C0 0F 85 ?? ?? ?? ??"
    ),
    # 文化组同化加成界面说明 0xf31bd0(玩家国家)：遍历政府改革找提供该组加成的改革(rdi)，+0x1FC 是找不到时清零 rdi 处
    "ASSIMGRPUI": (
        "4C 89 44 24 18 48 89 54 24 10 48 89 4C 24 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D AC 24 F8 "
        "FD FF FF 48 81 EC 08 03 00 00 48 8B F2 4C 8B C9 45 33 E4 44 89 64 24 40 4C 89 22 4C 89 62 10 48 "
        "C7 42 18 0F 00 00 00 44 88 22 4C 89 62 20 4C 89 62 30 48 C7 42 38 0F 00 00 00 44 88 62 20 C7 42 "
        "40 01 00 00 01 4C 89 62 44 41 BE 01 00 00 00"
    ),
    # 重新计算国家修正 0x92dff0 里的文化组同化加成：+0x376 累加组内省份总数(r14d 拥有数，r15d 总数，rbx 国家标签)，之后拥有数 < 总数不加
    "ASSIMMOD": (
        "48 89 5C 24 20 48 89 4C 24 08 55 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 D9 48 81 EC A0 00 00 "
        "00 4C 8B E1 48 8B 41 08 48 63 B8 60 28 00 00 E8 ?? ?? ?? ?? 85 FF 7E ?? 3B 78 14 7F ??"
    ),
    # 文化界面文化组条目 0xf31540：+0x20F 把拥有数 r14d / 总数 r15d 存到条目 +0x64 / +0x60(rdi 国家标签)
    "ASSIMVIEW": (
        "48 89 5C 24 10 4C 89 4C 24 20 4C 89 44 24 18 48 89 4C 24 08 55 56 57 41 54 41 55 41 56 41 57 48 "
        "8D AC 24 10 FF FF FF 48 81 EC F0 01 00 00 4D 8B F0 48 8B DA 48 8B F1 45 33 FF"
    ),
    # 条件工厂里令牌 0x3759 起的跳转表分支(含 has_reform 0x3852)：+0x14 起始令牌(取负)，+0x32 字节表，+0x39 偏移表
    "REFSWITCH": (
        "81 FA 6C 38 00 00 0F 8F ?? ?? ?? ?? 0F 84 ?? ?? ?? ?? 81 C2 A7 C8 FF FF 81 FA F9 00 00 00 0F 87 "
        "?? ?? ?? ?? 48 63 C2 48 8D 15 ?? ?? ?? ?? 0F B6 84 02 ?? ?? ?? ?? 8B 8C 82 ?? ?? ?? ?? 48 03 CA "
        "FF E1"
    ),
    # 某个政府改革自己的同化文化表 0x7b31b0(rcx 改革；+0x5B0 条件块，属性表里令牌 0x3855 assimilation_cultures)
    "REFASSIM": (
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8D 6C 24 F0 48 81 EC "
        "10 01 00 00 4C 8B F9 4C 63 81 BC 05 00 00 49 BD 78 32 4C 55 F1 64 79 FB 45 85 C0 0F 84 ?? ?? ?? "
        "??"
    ),
    # 执行决议命令的检查 0x48a6f0(按名字取决议，逐项求 allow +0xA8 / potential +0x30 的子条件)
    "DECVALID": (
        "48 89 5C 24 08 48 89 74 24 10 57 48 81 EC F0 00 00 00 48 8B D9 48 8D 51 70 48 8D 4C 24 20 E8 ?? "
        "?? ?? ?? 90 E8 ?? ?? ?? ?? 48 8D 53 50 48 8B C8 E8 ?? ?? ?? ?? 48 8B F0 48 85 C0 74 ??"
    ),
    # 可用决议提示 0xb5e10(遍历决议管理器 0x488160 的决议求条件)
    "DECALERT": (
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8D 6C 24 C9 48 81 EC "
        "F0 00 00 00 4C 8B E9 48 8B 41 20 4C 8B 05 ?? ?? ?? ?? 48 C1 E8 20 48 0F BF D0"
    ),
    # 决议界面 0xfcadf0(列出决议、求条件)
    "DECUI1": (
        "48 89 5C 24 10 48 89 6C 24 18 48 89 74 24 20 57 41 54 41 55 41 56 41 57 48 83 EC 60 4C 8B F1 48 "
        "8B B9 A0 00 00 00 48 8B 07 48 8B 98 80 01 00 00 45 33 ED 4C 89 6C 24 40 66 0F 6F 05 ?? ?? ?? ??"
    ),
    # 决议列表 0xfcb190(按 potential/allow 分类，+0x20C 重大决议)
    "DECUI2": (
        "48 8B C4 48 89 48 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D A8 C8 FD FF FF 48 81 EC F8 02 00 "
        "00 0F 29 70 A8 0F 29 78 98 48 8B F1 33 C0 89 85 48 02 00 00 89 85 50 02 00 00"
    ),
    # 决议详情 0xfcdb80(条件说明和求值)
    "DECVIEW": (
        "48 8B C4 48 89 48 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D A8 68 FE FF FF 48 81 EC 58 02 00 "
        "00 0F 29 70 A8 0F 29 78 98 48 8B F1 45 33 E4 45 8B EC 44 89 A5 A8 01 00 00 48 8B B9 A0 00 00 00"
    ),
    # 随机池抽取 0x9426d0 选完后、释放候选数组前(+0x2B：rdi 选中事件，[rbp-0x78] 候选事件，rbx 权重，r13 作用域，[rbp+0x178] 随机数)
    "POOLPICK": (
        "85 F6 7E ?? 8B C1 4D 8B 0C C0 2B 14 83 78 ?? FF C1 3B CE 7C ?? EB ?? 4C 8B CF 48 8B 5C 24 70 4C "
        "8B 45 88 49 8B F9 4D 85 C0 74 ?? 48 8B 44 24 58 49 2B C0 48 C1 F8 03"
    ),
    # 平均触发时间的本次触发概率(rcx 事件, rdx 作用域, r8d 检查间隔天数；百万分之一)：+0x17 call 算概率(事件 +0x7A8)，之后按国家大小缩放
    "MTTHCHANCE": (
        "48 89 5C 24 08 57 48 83 EC 20 48 8B D9 48 8B FA 48 81 C1 A8 07 00 00 E8 ?? ?? ?? ?? 4C 63 93 50 "
        "08 00 00 44 8B C0 45 85 D2 0F 8E ?? ?? ?? ??"
    ),
    # 国家平均触发时间回调(每个事件一次)：+0x5E 已触发过、+0xC6 掷骰、+0x152 能否触发、+0x16D 放进国家待触发列表([rbp+0x28] 事件，[rdi+0x10] 作用域)
    "MTTHLAMBDA": (
        "48 89 5C 24 08 44 89 44 24 18 48 89 54 24 10 55 56 57 48 8B EC 48 83 EC 70 48 8B DA 48 8B F9 48 "
        "8B 01 80 38 00 74 ?? 80 BA 60 0A 00 00 00 0F 85 ?? ?? ?? ?? 48 8B 82 98 07 00 00"
    ),
    # 省份平均触发时间(遍历事件管理器 +0x60 列表)：+0x2B 概率参数字节、+0xFA 取事件管理器、+0x3A3 放进省份待触发列表(rdi 事件，[rsp+0x40] 作用域)
    "PMTTHFN": (
        "48 89 54 24 10 48 89 4C 24 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 D8 48 81 EC 28 01 "
        "00 00 4C 8B F2 48 8B F1 45 32 ED 0F B6 05 ?? ?? ?? ?? 88 85 80 00 00 00 8B C8"
    ),
    # 事件说明 0x6ee110(事件, 字符串, 作用域, 选项序号)：+0x5F call 释放 std::string，+0xAC call 生成选项效果说明(选项, 字符串, 作用域, 0)
    "TIPFN": (
        "40 53 48 81 EC 90 00 00 00 48 8B DA 45 85 C9 79 ?? 48 81 C1 80 01 00 00 48 8B 01 33 D2 48 89 54 "
        "24 48 48 C7 44 24 60 0F 00 00 00 48 89 54 24 58 88 54 24 30 89 54 24 28 48 8D 54 24 48"
    ),
    # 条件工厂 0xb8ba90 里令牌 0x2965..0x2A5C 的跳转表分支(tag/OR/AND 等)：+0x14 起始令牌(取负)，+0x32 字节表，+0x39 偏移表
    "TRIGSWITCH": (
        "81 FA 69 2A 00 00 0F 8F ?? ?? ?? ?? 0F 84 ?? ?? ?? ?? 81 C2 9B D6 FF FF 81 FA F7 00 00 00 0F 87 "
        "?? ?? ?? ?? 48 63 C2 48 8D 15 ?? ?? ?? ?? 0F B6 84 02 ?? ?? ?? ?? 8B 8C 82 ?? ?? ?? ?? 48 03 CA "
        "FF E1"
    ),
    # “给继承人”效果(define_heir)的构造函数：+0x24 lea 出它的虚表
    "HEIRCTOR": (
        "48 89 5C 24 20 48 89 4C 24 08 55 56 57 41 56 41 57 48 81 EC 30 02 00 00 0F B6 EA 48 8B F9 E8 ?? "
        "?? ?? ?? 90 48 8D 05 ?? ?? ?? ?? 48 89 07 48 8D 4F 38 E8 ?? ?? ?? ?? 90 45 33 FF"
    ),
    # 效果块(选项 +0x50 等)的构造函数：+0x27 lea 出虚表，虚表 +0x78 是遍历子效果(+0x10..+0x18)的函数
    "EFFBLOCKCTOR": (
        "48 89 5C 24 20 48 89 4C 24 08 57 48 83 EC 40 48 8B D9 33 C0 48 89 41 10 48 89 41 18 48 89 41 20 "
        "C7 41 08 65 01 00 00 48 8D 05 ?? ?? ?? ?? 48 89 01 B8 FF FF FF FF 89 41 28 81 61 2C 00 F8 FF FF"
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
        "000000000000000000000000000000000000000000000000505152415041514152415348833dddffffff000f84"
        "8a000000e8950000004983faff747f488b4c2420e8f300000085c07471488b4c2418e8e500000085c07563488b"
        "4c24284885c97459534889e34883e4f04883ec20488b01ff50604889dc5b83f801753e488b4c241831c0488b89"
        "e80000004885c97408ffc0488b4910ebf3488b0d71ffffff48ff014801410848894120415b415a415941585a59"
        "58ff254dffffff415b415a415941585a5958e9ee00000049c7c2ffffffff488b0527ffffff488b004885c0743f"
        "488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889"
        "d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c35331"
        "c04885c974764889cb4c8b8b000100004c8b9b080100004d85c974244c89d84c29c8483d0020000077164d39d9"
        "7311498b5110e8aeffffff743d4983c120ebea4c8b8be800000041bb000400004d85c9742141ffcb741c498b01"
        "4885c0740e488b90d0000000e87bffffff740a4d8b4910ebda31c0eb05b8010000005bc3"
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
    "RETTRUE": (
        "00000000000000005152415250e82d0000004983faff7420488b4c24184885c97416488b5120e86a000000750b"
        "58415a5a59b801000000c358415a5a59eb6e49c7c2ffffffff488b05b3ffffff488b004885c0743f488b90601e"
        "000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e930"
        "80f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "POOLPICK": (
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000505351525655415041514152415341544155"
        "415641574881ec900000004989fc4c89eae87f01000085c00f8452010000488b05a9ffffff48ff004d85e47509"
        "48ff4008e93a0100004c89e14c89eae8e102000085c07510488b0582ffffff48ff4010e91b010000488b758848"
        "8b45904829f048c1f803483d000200007605b800020000488944246031c0488944242048894424284889442430"
        "488944243848894424404889442448488944245048894424584531ff4531f64c3b74246073454a8b0cf64885c9"
        "7437428b04b385c07e2f488b0509ffffff48ff40284c89eae85102000085c07518488b05f2feffff48ff40304c"
        "0fab7424204a6304b34901c749ffc6ebb44531e44d85ff750d488b05cdfeffff48ff4020eb69488b05c0feffff"
        "48ff40188b857801000069c0fd43030005c39e260089c1c1e91081e1ff7f000069c0fd43030005c39e2600c1e8"
        "1025ff7f0000c1e10f09c831d249f7f731c9483b4c2460731c480fa34c2420730f4863048b4829c279064c8b24"
        "ceeb0548ffc1ebdd4c89e74881c490000000415f415e415d415c415b415a415941585d5e5a595b58e9bf030000"
        "4989d1e84903000031c04983faff747d4d85c97478498d412c4180794b007404498d4144488b1031c04889d148"
        "c1e93884c9754e496349284885c97e50488b05cdfdffff488b004885c07441488b90981c0000482b90901c0000"
        "48c1fa034839d1732a488b90a81c00004885d2741e4869c9102e00004801d1488b916804000031c0e820030000"
        "0f94c00fb6c0c353565741544883ec2831c04885c974664189d4488b7108486379144881ff000100007749488d"
        "3cfe4839fe7340488b0e4885c97432488b01483b0574fdffff74304183fc037320483b056dfdffff7409483b05"
        "6cfdffff750e418d542401e89dffffff85c0750a4883c608ebbb31c0eb05b8010000004883c428415c5f5e5bc3"
        "53565741544883ec2831c04885c974654889cb4189d4488b7310488b7b184889f84829f0483d00080000774048"
        "39fe733b488b0e4885c9742d488b01483b05d8fcffff742b4183fc04731b488b4078483b05cdfcffff750e418d"
        "542401e89effffff85c0750a4883c608ebc031c0eb05b8010000004883c428415c5f5e5bc35356575541544155"
        "415641574883ec684989cc4989d531c04d85e40f84920100004d85ed0f8489010000498d4c244031d2e8cbfeff"
        "ff85c00f8573010000498bb42490070000498bbc24980700004829f748c1ff034883ff400f87520100004531f6"
        "4531ff4939fe0f833e0100004a8b1cf64885db0f842901000048638bbc000000488babb0000000488d4ccd0048"
        "894c2440483b6c24407320488b4d004885c974114c89ea488b01ff503084c00f84f00000004883c508ebd9488d"
        "4b5031d2e8bcfeffff85c00f85d7000000418b453c89442448418b45408944244c498b45184885c0740e8b483c"
        "894c24508b4840894c245431c048894424204889442428488944243048c74424380f0000004889d9488d542420"
        "4d89e84531c9ff156afbffff8b4424484189453c8b44244c41894540498b45184885c0740e8b4c245089483c8b"
        "4c2454894840488d4c242048837c2438107205488b4c2420488b54243031ed4883fa02722448ffca8039a77514"
        "80790147750583cd01eb0980790152750383cd0248ffc148ffca75df488d4c2420ff15fdfaffff83fd01740841"
        "bf01000000eb0849ffc6e9b9feffff4489f8eb0231c04883c468415f415e415d415c5d5f5e5bc349c7c2ffffff"
        "ff488b05b2faffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b9068"
        "1e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c974"
        "0948c1ea20664439d2c380c901c3"
    ),
    "CMTTHBLOCK": (
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000050515241504151415241"
        "53535641544883ec20488b5d28488b77104889f2e85700000085c074224889d94889f2e8d5010000488b0da7ff"
        "ffff85c0750648ff4138eb0648ff4140eb184883c420415c5e5b415b415a415941585a5958e9d80300004883c4"
        "20415c5e5b415b415a415941585a5958ff254effffff4989d1e84903000031c04983faff747d4d85c97478498d"
        "412c4180794b007404498d4144488b1031c04889d148c1e93884c9754e496349284885c97e50488b05f2feffff"
        "488b004885c07441488b90981c0000482b90901c000048c1fa034839d1732a488b90a81c00004885d2741e4869"
        "c9102e00004801d1488b916804000031c0e8200300000f94c00fb6c0c353565741544883ec2831c04885c97466"
        "4189d4488b7108486379144881ff000100007749488d3cfe4839fe7340488b0e4885c97432488b01483b05a1fe"
        "ffff74304183fc037320483b059afeffff7409483b0599feffff750e418d542401e89dffffff85c0750a4883c6"
        "08ebbb31c0eb05b8010000004883c428415c5f5e5bc353565741544883ec2831c04885c974654889cb4189d448"
        "8b7310488b7b184889f84829f0483d0008000077404839fe733b488b0e4885c9742d488b01483b0505feffff74"
        "2b4183fc04731b488b4078483b05fafdffff750e418d542401e89effffff85c0750a4883c608ebc031c0eb05b8"
        "010000004883c428415c5f5e5bc35356575541544155415641574883ec684989cc4989d531c04d85e40f849201"
        "00004d85ed0f8489010000498d4c244031d2e8cbfeffff85c00f8573010000498bb42490070000498bbc249807"
        "00004829f748c1ff034883ff400f87520100004531f64531ff4939fe0f833e0100004a8b1cf64885db0f842901"
        "000048638bbc000000488babb0000000488d4ccd0048894c2440483b6c24407320488b4d004885c974114c89ea"
        "488b01ff503084c00f84f00000004883c508ebd9488d4b5031d2e8bcfeffff85c00f85d7000000418b453c8944"
        "2448418b45408944244c498b45184885c0740e8b483c894c24508b4840894c245431c048894424204889442428"
        "488944243048c74424380f0000004889d9488d5424204d89e84531c9ff158ffcffff8b4424484189453c8b4424"
        "4c41894540498b45184885c0740e8b4c245089483c8b4c2454894840488d4c242048837c2438107205488b4c24"
        "20488b54243031ed4883fa02722448ffca8039a7751480790147750583cd01eb0980790152750383cd0248ffc1"
        "48ffca75df488d4c2420ff1522fcffff83fd01740841bf01000000eb0849ffc6e9b9feffff4489f8eb0231c048"
        "83c468415f415e415d415c5d5f5e5bc349c7c2ffffffff488b05d7fbffff488b004885c0743f488b90601e0000"
        "80b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f9"
        "07740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "PMTTHBLOCK": (
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000050515241504151415241"
        "53535641544883ec204889fb488db424b00000004889f2e85700000085c074224889d94889f2e8d5010000488b"
        "0da4ffffff85c0750648ff4138eb0648ff4140eb184883c420415c5e5b415b415a415941585a5958e9d8030000"
        "4883c420415c5e5b415b415a415941585a5958ff254bffffff4989d1e84903000031c04983faff747d4d85c974"
        "78498d412c4180794b007404498d4144488b1031c04889d148c1e93884c9754e496349284885c97e50488b05ef"
        "feffff488b004885c07441488b90981c0000482b90901c000048c1fa034839d1732a488b90a81c00004885d274"
        "1e4869c9102e00004801d1488b916804000031c0e8200300000f94c00fb6c0c353565741544883ec2831c04885"
        "c974664189d4488b7108486379144881ff000100007749488d3cfe4839fe7340488b0e4885c97432488b01483b"
        "059efeffff74304183fc037320483b0597feffff7409483b0596feffff750e418d542401e89dffffff85c0750a"
        "4883c608ebbb31c0eb05b8010000004883c428415c5f5e5bc353565741544883ec2831c04885c974654889cb41"
        "89d4488b7310488b7b184889f84829f0483d0008000077404839fe733b488b0e4885c9742d488b01483b0502fe"
        "ffff742b4183fc04731b488b4078483b05f7fdffff750e418d542401e89effffff85c0750a4883c608ebc031c0"
        "eb05b8010000004883c428415c5f5e5bc35356575541544155415641574883ec684989cc4989d531c04d85e40f"
        "84920100004d85ed0f8489010000498d4c244031d2e8cbfeffff85c00f8573010000498bb42490070000498bbc"
        "24980700004829f748c1ff034883ff400f87520100004531f64531ff4939fe0f833e0100004a8b1cf64885db0f"
        "842901000048638bbc000000488babb0000000488d4ccd0048894c2440483b6c24407320488b4d004885c97411"
        "4c89ea488b01ff503084c00f84f00000004883c508ebd9488d4b5031d2e8bcfeffff85c00f85d7000000418b45"
        "3c89442448418b45408944244c498b45184885c0740e8b483c894c24508b4840894c245431c048894424204889"
        "442428488944243048c74424380f0000004889d9488d5424204d89e84531c9ff158cfcffff8b4424484189453c"
        "8b44244c41894540498b45184885c0740e8b4c245089483c8b4c2454894840488d4c242048837c243810720548"
        "8b4c2420488b54243031ed4883fa02722448ffca8039a7751480790147750583cd01eb0980790152750383cd02"
        "48ffc148ffca75df488d4c2420ff151ffcffff83fd01740841bf01000000eb0849ffc6e9b9feffff4489f8eb02"
        "31c04883c468415f415e415d415c5d5f5e5bc349c7c2ffffffff488b05d4fbffff488b004885c0743f488b9060"
        "1e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e9"
        "3080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "MTTHCAP": (
        "00000000000000000000000000000000000000000000000048895c2408574883ec204889cb4889d74489442440"
        "e85600000089442438488d8ba80700004889fa448b442440ff15bdffffff837c243800743089c18b44244085c0"
        "7e2469c040420f0031d241b82107000041f7f03d40420f007605b840420f0039c10f4fc1eb0289c8ff2588ffff"
        "ff4989d1e88600000031c04983faff747d4d85c97478498d412c4180794b007404498d4144488b1031c04889d1"
        "48c1e93884c9754e496349284885c97e50488b0534ffffff488b004885c07441488b90981c0000482b90901c00"
        "0048c1fa034839d1732a488b90a81c00004885d2741e4869c9102e00004801d1488b916804000031c0e85d0000"
        "000f94c00fb6c0c349c7c2ffffffff488b05dcfeffff488b004885c0743f488b90601e000080b8661e00000775"
        "1080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea2044"
        "0fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "SUBJSLOT": (
        "000000000000000000000000000000005051524152e823000000488b57084983faff7412e86a000000750b415a"
        "5a5958ff25d2ffffff415a5a5958eb6e49c7c2ffffffff488b05b5ffffff488b004885c0743f488b90601e0000"
        "80b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f9"
        "07740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "SUBJPOWER": (
        "00000000000000005051524152e82b000000498b51204983faff741ae872000000751383bda001000000740ac7"
        "85a001000001000000415a5a5958eb6e49c7c2ffffffff488b05b5ffffff488b004885c0743f488b90601e0000"
        "80b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f9"
        "07740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "PERSPICK": (
        "0000000000000000000000000000000057e8ea000000488b95a0010000488b52204983faff7460e82a01000075"
        "5931ff4531c04d39e073104a8b0cc3e84c00000001c749ffc0ebeb85ff743b8b85a801000031d2f7f789d74531"
        "c04d39e073274a8b34c34889f1e82100000049ffc085c074e883ef0179e35f4c8d1dee0000004983c30e41ffe3"
        "5fe9e1000000415031c04885c974674c8b41584c6349644531d24c8b1d60ffffff4d8b1b4d85c97e45418b003d"
        "86020000742e488d144048c1e205410fb654135c84d27805f6c2407517418b400485c0740f0f9fc0d0ea80e201"
        "38d0750c41ffc24983c00849ffc9ebba31c0eb0831c04585d20f95c04158c349c7c2ffffffff488b05f2feffff"
        "488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1"
        "e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439"
        "d2c380c901c3"
    ),
    "DSTAT": (
        "0000000000000000505231c0488b91e80000004885d27408ffc0488b5210ebf3488b15d9ffffff48ff42104801"
        "4218488942285a58eb00"
    ),
    "TRADE50": (
        "00000000000000005051525356574150415141524153e8030100004983faff0f84b30000004c8b85200100004d"
        "85c00f84a30000004d8b48184d85c90f8496000000496370244d0fbfd24939f20f8385000000bbe80300004531"
        "db31ff4839f773174869cfc00000004c01c9e87a0000004901c348ffc7ebe44d85db7e5a4969cac00000004c01"
        "c9e85e0000004801c04c39d87e4331ff4839f7733c4c39d774324869cfc00000004c01c931c089415089415481"
        "7944e80300007e07c74144e803000081794ce80300007e07c7414ce803000048ffc7ebbf415b415a415941585f"
        "5e5b5a5958e989000000486341444863514c480fafc2489948f7fb4885c0790231c0486351484839d07e034889"
        "d0486351544801d0486351504829d0c349c7c2ffffffff488b05d4feffff488b004885c0743f488b90601e0000"
        "80b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f9"
        "07740848c1ea20440fb7d2c3"
    ),
    "LEADERMAX": (
        "000000000000000000000000000000005051524152e85a0000004983faff744d488b05e1ffffff488b004885c0"
        "743e488b80180100004885c074324d0fbfd24a8b04d04885c074254805d02800004c39f8751a415a5a5958448b"
        "55404989d84c8d1d640000004983c30741ffe3415a5a5958eb5649c7c2ffffffff488b057effffff488b004885"
        "c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974"
        "144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "MONFLAG": (
        "0000000000000000000000000000000000000000000000005051524152c784249400000000000000e83f000000"
        "4983faff74324c89c2e8870000007528488b44246831c9483b05bdffffff7409483b05bcffffff7505b9020000"
        "0083c901898c2494000000415a5a5958eb6e49c7c2ffffffff488b0586ffffff488b004885c0743f488b90601e"
        "000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e930"
        "80f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "MONPLUS": (
        "50518b84248400000089c183e10383f903751c418d4f040fa3c872138b064439f07d0c83c0024439f0410f4fc6"
        "89065958eb00"
    ),
    "MONGEN": (
        "c1fa0689d0c1e81f01c2f744247401000000740c83c202418d47040fab442474488d05060000004883c00affe0"
    ),
    "GOODSBEST": (
        "000000000000000000000000000000000000000000000000e815010000488b93680400004983faff0f84020100"
        "00e8550100007460488b93680400004889d048c1e83884c00f84e500000048c1ea20480fbfd2488b05b6ffffff"
        "488b004885c00f84ca000000488b80180100004885c00f84ba000000488b04d04885c00f84ad000000488b90f0"
        "180000e8f90000000f859b000000488b0564ffffff488b004885c00f84880000004c8b98d02500004d85db747c"
        "4d8b5b084d85db74734c8b4424304c8b4c243831c941ba000000804d39c8734e41837808007e41498b004885c0"
        "7439486350784885d27830486bd238418b541304488378280475154883783010730e817818676f6c647505baff"
        "ffff7f4439d27e064189d24889c14983c010ebad4885c974094889cfff25d8feffffeb6e49c7c2ffffffff488b"
        "05c0feffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e0000"
        "4889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1"
        "ea20664439d2c380c901c3"
    ),
    "GOODSBEST2": (
        "000000000000000000000000000000000000000000000000e815010000498b95680400004983faff0f84020100"
        "00e8550100007460498b95680400004889d048c1e83884c00f84e500000048c1ea20480fbfd2488b05b6ffffff"
        "488b004885c00f84ca000000488b80180100004885c00f84ba000000488b04d04885c00f84ad000000488b90f0"
        "180000e8f90000000f859b000000488b0564ffffff488b004885c00f84880000004c8b98d02500004d85db747c"
        "4d8b5b084d85db74734c8b4424404c8b4c244831c941ba000000804d39c8734e41837808007e41498b004885c0"
        "7439486350784885d27830486bd238418b541304488378280475154883783010730e817818676f6c647505baff"
        "ffff7f4439d27e064189d24889c14983c010ebad4885c974094889caff25d8feffffeb6e49c7c2ffffffff488b"
        "05c0feffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e0000"
        "4889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d148c1e93884c9740948c1"
        "ea20664439d2c380c901c3"
    ),
    "RETSCOPE": (
        "0000000000000000515241514152e814000000415a41595a5985c00f8402010000b801000000c34989d1e88600"
        "000031c04983faff747d4d85c97478498d412c4180794b007404498d4144488b1031c04889d148c1e93884c975"
        "4e496349284885c97e50488b0595ffffff488b004885c07441488b90981c0000482b90901c000048c1fa034839"
        "d1732a488b90a81c00004885d2741e4869c9102e00004801d1488b916804000031c0e85d0000000f94c00fb6c0"
        "c349c7c2ffffffff488b053dffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000"
        "007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c34889d1"
        "48c1e93884c9740948c1ea20664439d2c380c901c3"
    ),
    "PRIMC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b04d14989d04989c3e80e01"
        "00004983faff742b4d39d075264d398b281200007414e8280000004885c0750a4883fc00ff25b1ffffff4839c0"
        "ff25a8ffffff4c89d84c398828120000ff2598ffffff488b0589ffffff488b004885c07465488b88981c000048"
        "2b88901c000048c1f903488b90a81c00004885d2744741b8010000004939c8733c4d69d8102e00004901d3498b"
        "836804000048c1e83884c0741d498b836804000048c1e820664439d0750c498b83400400004c39c8740549ffc0"
        "ebc0c331c0c349c7c2ffffffff488b050bffffff488b004885c0743f488b90601e000080b8661e000007751080"
        "b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7"
        "d2c3e8a5ffffff4983faff740ee80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508000000"
        "488d86001800004839f8480f42f8488b1d9bfeffff4885db74434839fe733e488b06482b057ffeffff4889c148"
        "c1e920751e488b0b488d53084885c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b8010000"
        "00eb0231c05f5e5b85c0c3"
    ),
    "GROUPC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b04d1488b88281200005152"
        "e8060100005a594983faff74224c39d2751d4885c974094c398988000000740f51e80f000000594885c0740348"
        "89c1ff25a6ffffff488b0597ffffff488b004885c0746e488b88981c0000482b88901c000048c1f903488b90a8"
        "1c00004885d2745041b8010000004939c873454d69d8102e00004901d3498b836804000048c1e83884c0742649"
        "8b836804000048c1e820664439d07515498b83400400004885c074094c398888000000740549ffc0ebb7c331c0"
        "c349c7c2ffffffff488b0510ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000"
        "007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3e8a5ff"
        "ffff4983faff740ee80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508000000488d860018"
        "00004839f8480f42f8488b1da0feffff4885db74434839fe733e488b06482b0584feffff4889c148c1e920751e"
        "488b0b488d53084885c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b801000000eb0231c0"
        "5f5e5b85c0c3"
    ),
    "OWNC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b882812000051524150e879"
        "00000041585a594983faff740c4c39d27507498b884004000049398840040000ff25b5ffffff49c7c2ffffffff"
        "488b059fffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e"
        "00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3e8a5ffffff4983faff740e"
        "e80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508000000488d86001800004839f8480f42"
        "f8488b1d2fffffff4885db74434839fe733e488b06482b0513ffffff4889c148c1e920751e488b0b488d530848"
        "85c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b801000000eb0231c05f5e5b85c0c3"
    ),
    "RELC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b04d14989d150e813010000"
        "584989c34983faff742b4d39d175264d3983300f00007414e8280000004885c0750a4883fc00ff25afffffff48"
        "39c0ff25a6ffffff4c89d84c3980300f0000ff2596ffffff488b0587ffffff488b004885c07466488b88981c00"
        "00482b88901c000048c1f903488b90a81c00004885d2744849c7c1010000004939c9733c4d69d9102e00004901"
        "d3498b836804000048c1e83884c0741d498b836804000048c1e820664439d0750c498b83480400004c39c07405"
        "49ffc1ebc0c331c0c349c7c2ffffffff488b0508ffffff488b004885c0743f488b90601e000080b8661e000007"
        "751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20"
        "440fb7d2c3e8a5ffffff4983faff740ee80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508"
        "000000488d86001800004839f8480f42f8488b1d98feffff4885db74434839fe733e488b06482b057cfeffff48"
        "89c148c1e920751e488b0b488d53084885c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b8"
        "01000000eb0231c05f5e5b85c0c3"
    ),
    "RGROUPC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b04d1488b88300f00005152"
        "e8070100005a594983faff74224c39d2751d4885c974094c3989d8000000740f51e80f000000594885c0740348"
        "89c1ff25a6ffffff488b0597ffffff488b004885c0746f488b88981c0000482b88901c000048c1f903488b90a8"
        "1c00004885d2745149c7c0010000004939c873454d69d8102e00004901d3498b836804000048c1e83884c07426"
        "498b836804000048c1e820664439d07515498b83480400004885c074094c3988d8000000740549ffc0ebb7c331"
        "c0c349c7c2ffffffff488b050fffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e00"
        "00007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3e8a5"
        "ffffff4983faff740ee80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508000000488d8600"
        "1800004839f8480f42f8488b1d9ffeffff4885db74434839fe733e488b06482b0583feffff4889c148c1e92075"
        "1e488b0b488d53084885c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b801000000eb0231"
        "c05f5e5b85c0c3"
    ),
    "OWNRC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b88300f000051524150e879"
        "00000041585a594983faff740c4c39d27507498b884804000049398848040000ff25b5ffffff49c7c2ffffffff"
        "488b059fffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e"
        "00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3e8a5ffffff4983faff740e"
        "e80a000000750749c7c2ffffffffc3535657488d74241865488b3c2508000000488d86001800004839f8480f42"
        "f8488b1d2fffffff4885db74434839fe733e488b06482b0513ffffff4889c148c1e920751e488b0b488d530848"
        "85c974123b0272053b4204720f4883c20848ffc9ebe94883c608ebc4b801000000eb0231c05f5e5b85c0c3"
    ),
    "ASSIMC": (
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000eb2000000000000000006d756768616c5f676f7665726e6d656e7400000000000000488b0424483b05b3"
        "ffffff0f843201000051524152e8d30000004c89d0415a5a594883f8ff0f84180100004c8b1d84ffffff4d8b1b"
        "4d85db0f84050100004d8b9b180100004d85db0f84f50000004d8b1cc34d85db0f84e80000004d8b9b20140000"
        "4d85db0f84d80000004983c3384939cb0f85cb000000488b0561ffffff4885c0756853514883ec48ff1536ffff"
        "ff4885c074454889c1488d542420488d0544ffffff48890248c742080000000048c742101100000048c742181f"
        "000000ff1509ffffff4885c074104889c1ff1503ffffff48890506ffffff4883c448595b488b05f9feffff4885"
        "c07457c349c7c2ffffffff488b05b3feffff488b004885c0743f488b90601e000080b8661e000007751080b86f"
        "1e0000007407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "ASSIMONE": (
        "0000000000000000000000000000000051524152e8340000004983faff741a488b85800000004885c0740e488b"
        "402048c1e820664439d0740341ffc7415a5a59488b8668040000ff25bbffffff49c7c2ffffffff488b05a5ffff"
        "ff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148"
        "c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "ASSIMGRP": (
        "000000000000000000000000000000008b45645152415250e835000000584983faff7421498b4c24184885c974"
        "17488b492048c1e920664439d17509837d60007e038b4560415a5a59394560ff25b6ffffff49c7c2ffffffff48"
        "8b05a0ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00"
        "004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "ASSIMUI": (
        "000000000000000000000000000000000000000000000000eb2000000000000000006d756768616c5f676f7665"
        "726e6d656e74000000000000004531e44489e7488b05d3ffffff4885c0756750515241504151415241534883ec"
        "48ff159fffffff4885c074374889c1488d542420488d05adffffff48890248c742080000000048c74210110000"
        "0048c742181f000000ff1572ffffff4889057dffffff4883c448415b415a415941585a5958488b0567ffffff48"
        "89c7ff2554ffffff"
    ),
    "ASSIMMODC": (
        "000000000000000000000000000000005152415250e836000000584983faff741d4889d948c1e920664439d175"
        "104589f74585ff750c41bf01000000eb0444037d6b415a5a59488b4710ff25b8ffffff49c7c2ffffffff488b05"
        "a2ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e000048"
        "89d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "ASSIMVIEWC": (
        "000000000000000000000000000000005152415250e82e000000584983faff74154889f948c1e920664439d175"
        "084585f674034589f7415a5a594489766444897e60ff25c0ffffff49c7c2ffffffff488b05aaffffff488b0048"
        "85c0743f488b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c9"
        "74144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "CULTLISTC": (
        "0000000000000000000000000000000000000000000000000000000000000000488b442438483b05dcffffff75"
        "32e83d0000004983faff7427488b4424584885c0741d488b40184885c07414488b402048c1e820664439d07506"
        "ff25b0ffffff488b8ea0080000488b01ff25a8ffffff49c7c2ffffffff488b0582ffffff488b004885c0743f48"
        "8b90601e000080b8661e000007751080b86f1e0000007407488b90681e00004889d148c1e93884c974144889d1"
        "48c1e93080f907740848c1ea20440fb7d2c3"
    ),
    "TECHC": (
        "0000000000000000000000000000000052e8440000005a4983faff7432b844000000b92c000000807f4b000f44"
        "c1488b04384889c148c1e93884c9741248c1e820664439d07508b001ff25b9ffffff39d60f9dc0ff25aeffffff"
        "49c7c2ffffffff488b0598ffffff488b004885c0743f488b90601e000080b8661e000007751080b86f1e000000"
        "7407488b90681e00004889d148c1e93884c974144889d148c1e93080f907740848c1ea20440fb7d2c3"
    ),
}

# (名称, 特征码键, 挂钩处相对命中处的偏移, 被搬走的原指令长度, 机器码键, 数据区各项)
# 数据区：glob = 游戏对象全局指针，dside = 战斗骰子面数(COMBAT_DICE_SIDE)，wipe = 原版全歼函数，
#         direct = 召集循环里“盟友直接加入战争”的分支，
#         evposs = 事件能否触发(原版)，optip = 生成选项效果说明，strfree = 释放 std::string，
#         cskip/pskip = 平均触发时间事件放进待触发列表之后的位置(坏事件不放时跳过去)，
#         cultskip = 省份文化修正里“主文化，不加惩罚”的出口
HOOKS = (
    ("战斗骰子(新阶段)", "LANDDICE1", 0x12, 6, "DICE14", ("glob", "dside")),
    ("战斗骰子(阶段内)", "LANDDICE2", 0x12, 8, "DICE14", ("glob", "dside")),
    ("强攻骰子", "ASSAULTDICE", 0x22, 6, "DICE13", ("glob", "dside")),
    ("围城骰子", "SIEGEDICE", 0x32, 7, "SIEGE", ("glob",)),
    # 全歼(已关闭，去掉下面两行开头的 # 即恢复)
    # ("玩家打赢陆战即全歼", "ENDBATTLE", 0, 5, "WIPE", ("glob", "wipe", "@wipestats")),
    # ("全歼统计：消灭部队时还剩几支", "DESTROYSIDE", 0, 5, "DSTAT", ("@wipestats",)),
    ("宣战无稳定度等惩罚", "STABHIT", 0, 5, "NOPENALTY", ("glob",)),
    ("宣战不加侵略扩张", "DOWAE", 0x25, 5, "NOAE", ("glob",)),
    # 盟友必定参战(已关闭，去掉下面两行开头的 # 即恢复)
    # ("盟友像附庸一样直接参战", "ALLYJOIN", 0x30, 5, "ALLYJOIN", ("glob", "direct")),
    # ("参战方列表里盟友算强制参战", "ALLYFLAG", 0x1D, 5, "ALLYFLAG", ("glob",)),
    ("玩家在影响力超过一半的贸易节点占 100%", "TRADE50SITE", 0x3B, 7, "TRADE50", ("glob",)),
    ("玩家(及属国)殖民地商品取价格最高的", "GOODSROLL", 0x98, 5, "GOODSBEST", ("glob", "goodsback", "cmgr")),
    ("玩家(及属国)殖民地商品取价格最高的(殖民地成长时)", "GOODSGROW", 0x54, 5, "GOODSBEST2", ("glob", "goodsback2", "cmgr")),
    ("玩家的将领能力点总数取最大", "LEADERGEN", 0x29, 7, "LEADERMAX", ("glob", "cmgr")),
    ("玩家部队无损耗", "ATTRITION", 0, 5, "ATTR", ("glob",)),
    ("玩家舰队无损耗", "NAVALATTR", 0, 5, "ATTR", ("glob",)),
    ("随机池：抽中坏事件就在同池好事件里重抽", "POOLPICK", 0x2B, 5, "POOLPICK",
     ("glob", "optip", "strfree", "heirvt", "blockiter", "@evstats", "tagvt", "orvt", "andvt")),
    ("平均触发时间：玩家的事件最多 5 年", "MTTHCHANCE", 0, 5, "MTTHCAP", ("glob", "mtthfn", "mtthback")),
    ("国家平均触发时间：坏事件不发生", "MTTHLAMBDA", 0x16D, 11, "CMTTHBLOCK",
     ("glob", "optip", "strfree", "cskip", "heirvt", "blockiter", "@evstats", "tagvt", "orvt", "andvt")),
    ("省份平均触发时间：坏事件不发生", "PMTTHFN", 0x3A3, 7, "PMTTHBLOCK",
     ("glob", "optip", "strfree", "pskip", "heirvt", "blockiter", "@evstats", "tagvt", "orvt", "andvt")),
    ("玩家的属国不占外交关系(同撤销上诉特权后的帝国诸侯)", "SUBJSLOT", 0xE, 8, "SUBJSLOT", ("glob", "slotno")),
    ("玩家属国的相对实力只算自己(同撤销上诉特权后的帝国诸侯)", "SUBJPOWER", 0x14, 7, "SUBJPOWER", ("glob",)),
    ("玩家的统治者/继承人/配偶只出好特质", "PERSPICK", 0x18, 14, "PERSPICK", ("glob", "modtab")),
    ("统治者/继承人/配偶三维 +2：记下是不是玩家", "MONSKILL", 0x47, 8, "MONFLAG", ("glob", "defret1", "defret2")),
    ("统治者/继承人/配偶三维 +2：随机生成的加点", "MONSKILL", 0x21C, 10, "MONGEN", ()),
    ("统治者/继承人/配偶三维 +2：脚本指定的加点", "MONSKILL", 0x24A, 7, "MONPLUS", ()),
    ("所有宗教都按本教算", "RELOWN", 0, 5, "RETTRUE", ("glob",)),
    ("所有宗教都算融合宗教(secondary_religion)", "SECREL", 0, 6, "RETSCOPE", ("glob",)),
    ("所有宗教组都算融合宗教(secondary_religion_group)", "SECRELGRP", 0, 6, "RETSCOPE", ("glob",)),
    ("有的文化算主文化(primary_culture)", "PRIMCULT", 0x38B, 11, "PRIMC", ("glob", "primback", "imgbase", "@decfns")),
    ("有的文化算主文化(culture_group)", "CULTGRP", 0x63, 11, "GROUPC", ("glob", "grpback", "imgbase", "@decfns")),
    ("有的文化算主文化(has_owner_culture)", "@OWNCULT", 0, 14, "OWNC", ("glob", "ownback", "imgbase", "@decfns")),
    ("有的宗教算国教(religion)", "RELIG", 0x95, 11, "RELC", ("glob", "relback", "imgbase", "@decfns")),
    ("有的宗教算国教(religion_group)", "RELGRP", 0x67, 11, "RGROUPC", ("glob", "rgrpback", "imgbase", "@decfns")),
    ("有的宗教算国教(has_owner_religion)", "@OWNREL", 0, 14, "OWNRC", ("glob", "ownrback", "imgbase", "@decfns")),
    ("玩家的科技条件(adm_tech 等)一律为真", "TECHTRIG", 0x3E, 5, "TECHC", ("glob", "techback")),
    ("玩家始终有莫卧儿底万(同化文化)", "ASSIMGET", 0, 6, "ASSIMC", ("glob", "cmgr", "assimskip", "refmgr", "reffind", "refassim")),
    ("底万：玩家有一个省份就同化文化", "ASSIMPROC", 0x13D, 10, "ASSIMONE", ("glob", "aoneback")),
    ("底万：玩家新大陆省份的文化也同化", "CULTLIST", 0x5B, 10, "CULTLISTC", ("glob", "clret", "clkeep", "clback")),
    ("底万：玩家有一个省份就得文化组加成", "ASSIMPROC", 0x707, 6, "ASSIMGRP", ("glob", "agrpback")),
    ("底万：文化组加成的界面说明按莫卧儿政体", "ASSIMGRPUI", 0x1FC, 6, "ASSIMUI", ("refmgr", "reffind", "auiback")),
    ("底万：玩家有一个省份就加文化组修正", "ASSIMMOD", 0x376, 8, "ASSIMMODC", ("glob", "amodback")),
    ("底万：文化界面里玩家有一个省份的文化组按已同化显示", "ASSIMVIEW", 0x20F, 8, "ASSIMVIEWC", ("glob", "aviewback")),
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


# 补丁内存里的数据块：键 -> 函数(img)，安装时生成
# @evstats：事件统计计数(玩家的)，9 个 qword，含义见 EVSTATS_NAMES
# 决议相关函数(栈上有它们的返回地址就算“在决议里”)：(特征码键, 函数长度)。“有就算”的文化/宗教条件只在这些函数里生效。
DECFNS = (("DECVALID", 0xD3), ("DECALERT", 0x386), ("DECUI1", 0x31F), ("DECUI2", 0x14EE), ("DECVIEW", 0xBCD))


def decfns_blob(img):
    rows = []
    for k, size in DECFNS:
        hits = img.find_sig(SIGS[k])
        if len(hits) == 1:
            rows.append(struct.pack("<II", hits[0], hits[0] + size))
    return struct.pack("<Q", len(rows)) + b"".join(rows)


BLOBS = {"@evstats": lambda img: bytes(8 * 9), "@wipestats": lambda img: bytes(8 * 6), "@decfns": decfns_blob}
WIPESTATS_NAMES = ("全歼次数", "全歼开始时败方部队累计", "原版消灭部队函数调用次数", "调用时败方部队累计",
                   "最近一次全歼开始时败方部队数", "最近一次消灭时败方部队数")
EVSTATS_NAMES = ("随机池抽取次数", "原版就是空抽", "抽中好事件(保留)", "抽中坏事件、重抽成功",
                 "抽中坏事件、池里没好事件(变成空抽)", "重抽时看过的候选", "其中好事件",
                 "平均触发时间事件放行", "平均触发时间事件拦下")

def rel_target(img, insn_rva, disp_off, insn_len):
    return insn_rva + insn_len + struct.unpack("<i", img.bytes_at(insn_rva + disp_off, 4))[0]


def trig_vtable(img, sigkey, tok):
    """条件工厂里按令牌跳转的一段(特征码 sigkey：+0x14 add edx,-起始令牌，+0x1A cmp edx,最大序号，+0x32 字节表，
    +0x39 偏移表，表里是相对映像的偏移)：令牌 tok 的分支是 lea rcx,[rbp+0x30]; call 构造函数，构造函数里第一个
    lea rax,[rip+X]; mov [rbx],rax 的 X 是这个条件类的虚表(rva)。找不到返回 None。"""
    hits = img.find_sig(SIGS[sigkey])
    if len(hits) != 1:
        return None
    h = hits[0]
    lo = -struct.unpack("<i", img.bytes_at(h + 0x14, 4))[0]
    hi = struct.unpack("<I", img.bytes_at(h + 0x1A, 4))[0]
    btab = struct.unpack("<I", img.bytes_at(h + 0x32, 4))[0]
    dtab = struct.unpack("<I", img.bytes_at(h + 0x39, 4))[0]
    i = tok - lo
    if not 0 <= i <= hi:
        return None
    case = struct.unpack("<I", img.bytes_at(dtab + 4 * img.bytes_at(btab + i, 1)[0], 4))[0]
    if img.bytes_at(case, 5) != b"\x48\x8d\x4d\x30\xe8":
        return None
    ctor = rel_target(img, case + 4, 1, 5)
    j = re.search(rb"\x48\x8d\x05(....)\x48\x89\x03", img.bytes_at(ctor, 0x100), re.S)
    return rel_target(img, ctor + j.start(), 3, 7) if j else None


def pe_image_base(img):
    pe = struct.unpack_from("<I", img.data, 0x3C)[0]
    return struct.unpack_from("<Q", img.data, pe + 24 + 24)[0]


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
        if sk.startswith("@"):
            continue
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
    # “给继承人”效果(define_heir)的虚表：它的构造函数(特征码 HEIRCTOR) +0x24 的 lea；
    # 效果块的遍历函数：效果块构造函数(EFFBLOCKCTOR) +0x27 lea 出的虚表里 +0x78 那一项(虚表里存的是绝对地址)
    for k, sk, off in (("heirvt", "HEIRCTOR", 0x24), ("@blockvt", "EFFBLOCKCTOR", 0x27)):
        hits = img.find_sig(SIGS[sk])
        if len(hits) == 1 and img.bytes_at(hits[0] + off, 3) == b"\x48\x8d\x05":
            R[k] = rel_target(img, hits[0] + off, 3, 7)
        else:
            problems.append(f"{k}: 特征码未命中")
    if "@blockvt" in R:
        pe = struct.unpack_from("<I", img.data, 0x3C)[0]
        image_base = struct.unpack_from("<Q", img.data, pe + 24 + 24)[0]
        R["blockiter"] = struct.unpack("<Q", img.bytes_at(R.pop("@blockvt") + 0x78, 8))[0] - image_base
    # 修正定义数组(每项 0x60，+0x5C 标志)的起始指针变量：修正登记函数(MODREG) +0x19 的 sub rax,[rip+X]
    hits = img.find_sig(SIGS["MODREG"])
    if len(hits) == 1 and img.bytes_at(hits[0] + 0x19, 3) == b"\x48\x2b\x05":
        R["modtab"] = rel_target(img, hits[0] + 0x19, 3, 7)
    else:
        problems.append("修正定义数组: 特征码未命中")
    # 条件 tag / OR / AND 的虚表：条件工厂里令牌 0x2965 起的跳转表(特征码 TRIGSWITCH：add edx,-0x2965 后
    # movzx eax,[基址+rax+字节表]、mov ecx,[基址+rax*4+偏移表]，表里是相对映像的偏移)；对应分支是
    # lea rcx,[rbp+0x30]; call 构造函数，构造函数里第一个 lea rax,[rip+X]; mov [rbx],rax 的 X 就是虚表
    for k, tok in (("tagvt", 0x2A05), ("orvt", 0x296C), ("andvt", 0x296B)):
        v = trig_vtable(img, "TRIGSWITCH", tok)
        if v is not None:
            R[k] = v
    for k in ("tagvt", "orvt", "andvt"):
        if k not in R:
            problems.append(f"{k}: 找不到(国家专属事件不拦截失效)")
    # 国家管理器指针变量(+0x118 国家数组)：平均触发时间概率函数(MTTHCHANCE) +0x44 的 mov rcx,[rip+X]
    e = R.get("entry:平均触发时间：玩家的事件最多 5 年")
    if e is not None and img.bytes_at(e + 0x44, 3) == b"\x48\x8b\x0d":
        R["cmgr"] = rel_target(img, e + 0x44, 3, 7)
    elif e is not None:
        problems.append("国家管理器: 找不到")
    # 事件/决议效果(define_ruler 等)对临时三维调用生成函数那两处 call 之后的地址(DEFRULERGEN +0x4B，DEFHEIRGEN +0x4E)
    for k, sk, off in (("defret1", "DEFRULERGEN", 0x4B), ("defret2", "DEFHEIRGEN", 0x4E)):
        hits = img.find_sig(SIGS[sk])
        if len(hits) == 1 and img.bytes_at(hits[0] + off - 5, 1) == b"\xe8":
            R[k] = hits[0] + off
        else:
            problems.append(f"{k}: 特征码未命中")
    # 殖民地确定商品：随机抽取处(+0x98)之后，设置商品前 mov byte [rsp+0x20],1 的位置(+0xDB)
    e = R.get("entry:玩家殖民地商品取价格最高的")
    if e is not None and img.bytes_at(e + 0x43, 5) == bytes.fromhex("c644242001"):
        R["goodsback"] = e + 0x43
    elif e is not None:
        problems.append("殖民地商品: 结构不符")
    # 殖民地成长时确定商品：随机抽取处(+0x54)之后，设置商品前 mov byte [rsp+0x20],1 的位置(+0x9B)
    e = R.get("entry:玩家殖民地商品取价格最高的(殖民地成长时)")
    if e is not None and img.bytes_at(e + 0x47, 5) == bytes.fromhex("c644242001"):
        R["goodsback2"] = e + 0x47
    elif e is not None:
        problems.append("殖民地商品(成长时): 结构不符")
    # has_owner_culture 的判断函数：条件工厂里令牌 0x2BD7(has_owner_culture) 那段跳转表(特征码 OWNSWITCH)解出虚表，
    # 判断函数在虚表 +0x30(这类条件函数开头都是同样的报错代码，按特征码找不唯一)
    v = trig_vtable(img, "OWNSWITCH", 0x2BD7)
    if v is not None:
        ev = struct.unpack("<Q", img.bytes_at(v + 0x30, 8))[0] - pe_image_base(img)
        R["entry:有的文化算主文化(has_owner_culture)"] = ev + 0x31C
        R["ownback"] = ev + 0x31C + 14
    else:
        problems.append("has_owner_culture: 找不到")
    # has_owner_religion：同一段跳转表里令牌 0x2BD6，判断函数结构同 has_owner_culture(挂钩处也在 +0x31C)
    v = trig_vtable(img, "OWNSWITCH", 0x2BD6)
    if v is not None:
        ev = struct.unpack("<Q", img.bytes_at(v + 0x30, 8))[0] - pe_image_base(img)
        R["entry:有的宗教算国教(has_owner_religion)"] = ev + 0x31C
        R["ownrback"] = ev + 0x31C + 14
    else:
        problems.append("has_owner_religion: 找不到")
    # primary_culture / culture_group 挂钩处之后跳回的位置
    e = R.get("entry:有的文化算主文化(primary_culture)")
    if e is not None:
        R["primback"] = e + 11
    e = R.get("entry:有的文化算主文化(culture_group)")
    if e is not None:
        R["grpback"] = e + 11
    R["imgbase"] = 0
    e = R.get("entry:玩家的科技条件(adm_tech 等)一律为真")
    if e is not None:
        R["techback"] = e + 5
    e = R.get("entry:底万：玩家新大陆省份的文化也同化")
    a = R.get("entry:底万：玩家有一个省份就同化文化")
    hits = img.find_sig(SIGS["ASSIMPROC"])
    if e is not None and len(hits) == 1 and img.bytes_at(hits[0] + 0x86, 1) == b"\xe8" \
            and rel_target(img, hits[0] + 0x86, 1, 5) == e - 0x5B:
        R["clret"] = hits[0] + 0x8B
        R["clkeep"] = e + 0x15
        R["clback"] = e + 10
    elif e is not None:
        problems.append("同化处理里列出文化的调用: 找不到")
    e = R.get("entry:底万：玩家有一个省份就加文化组修正")
    if e is not None:
        R["amodback"] = e + 8
    e = R.get("entry:底万：文化界面里玩家有一个省份的文化组按已同化显示")
    if e is not None:
        R["aviewback"] = e + 8
    e = R.get("entry:底万：文化组加成的界面说明按莫卧儿政体")
    if e is not None:
        R["auiback"] = e + 6
    e = R.get("entry:底万：玩家有一个省份就同化文化")
    if e is not None:
        R["aoneback"] = e + 10
    e = R.get("entry:底万：玩家有一个省份就得文化组加成")
    if e is not None:
        R["agrpback"] = e + 6
    for k, _size in DECFNS:
        if len(img.find_sig(SIGS[k])) != 1:
            problems.append(f"决议函数 {k}: 特征码未命中")
    e = R.get("entry:有的宗教算国教(religion)")
    if e is not None:
        R["relback"] = e + 11
    e = R.get("entry:有的宗教算国教(religion_group)")
    if e is not None:
        R["rgrpback"] = e + 11
    # 外交关系占不占名额：挂钩处 +0x44 是原版“皇帝的帝国诸侯不占”返回否的 xor al,al; jmp
    e = R.get("entry:玩家的属国不占外交关系(同撤销上诉特权后的帝国诸侯)")
    if e is not None and img.bytes_at(e + 0x44, 3) == b"\x32\xc0\xe9":
        R["slotno"] = e + 0x44
    elif e is not None:
        problems.append("外交关系名额函数: 结构不符")
    # 平均触发时间概率函数(MTTHCHANCE)：+0x17 call 算概率的函数，+0x1C 是之后的 movsxd r10,[rbx+0x850]
    e = R.get("entry:平均触发时间：玩家的事件最多 5 年")
    if e is not None and img.bytes_at(e + 0x17, 1) == b"\xe8" and img.bytes_at(e + 0x1C, 7) == bytes.fromhex("4c639350080000"):
        R["mtthfn"] = rel_target(img, e + 0x17, 1, 5)
        R["mtthback"] = e + 0x1C
    elif e is not None:
        problems.append("平均触发时间概率函数: 结构不符")
    # 平均触发时间的事件放进待触发列表之后的位置(坏事件不放时跳过去)：国家回调(MTTHLAMBDA) +0x181，
    # 省份函数(PMTTHFN) +0x428
    e = R.get("entry:国家平均触发时间：坏事件不发生")
    if e is not None:
        R["cskip"] = e - 0x16D + 0x181
    e = R.get("entry:省份平均触发时间：坏事件不发生")
    if e is not None:
        R["pskip"] = e - 0x3A3 + 0x428
    # 生成选项效果说明、释放字符串：事件说明函数(特征码 TIPFN)里 +0xAC、+0x5F 两处 call 的目标
    hits = img.find_sig(SIGS["TIPFN"])
    if len(hits) == 1 and img.bytes_at(hits[0] + 0xAC, 1) == b"\xe8" and img.bytes_at(hits[0] + 0x5F, 1) == b"\xe8":
        R["optip"] = rel_target(img, hits[0] + 0xAC, 1, 5)
        R["strfree"] = rel_target(img, hits[0] + 0x5F, 1, 5)
    else:
        problems.append("生成选项说明的函数: 特征码未命中")
    # 莫卧儿底万：文化组联合判断(ASSIMCG)+0x47 call 取同化表，返回地址 +0x4C；改革管理器和按名字查改革从 has_reform
    # (令牌 0x3852，跳转表 REFSWITCH)条件的读取函数(虚表 +0x68)里 +0x24、+0x33 两处 call 取；改革的同化表 REFASSIM
    e = R.get("entry:玩家始终有莫卧儿底万(同化文化)")
    hits = img.find_sig(SIGS["ASSIMCG"])
    if e is not None and len(hits) == 1 and img.bytes_at(hits[0] + 0x47, 1) == b"\xe8" and rel_target(img, hits[0] + 0x47, 1, 5) == e:
        R["assimskip"] = hits[0] + 0x4C
    else:
        problems.append("文化组联合判断: 找不到")
    v = trig_vtable(img, "REFSWITCH", 0x3852)
    rd = struct.unpack("<Q", img.bytes_at(v + 0x68, 8))[0] - pe_image_base(img) if v is not None else None
    if rd is not None and img.bytes_at(rd + 0x24, 1) == b"\xe8" and img.bytes_at(rd + 0x33, 1) == b"\xe8":
        R["refmgr"] = rel_target(img, rd + 0x24, 1, 5)
        R["reffind"] = rel_target(img, rd + 0x33, 1, 5)
    else:
        problems.append("has_reform 读取函数: 找不到")
    hits = img.find_sig(SIGS["REFASSIM"])
    if len(hits) == 1:
        R["refassim"] = hits[0]
    else:
        problems.append("改革的同化表: 特征码未命中")
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


def attach(wait_seconds, stop=None):
    """等 eu4.exe 出现且代码段可读。返回 (h, base, img, pid)；超时返回 None；stop() 为真时返回 "stop"。"""
    deadline = time.time() + wait_seconds
    img = None
    while time.time() < deadline:
        if stop is not None and stop():
            return "stop"
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
            e = R.get("entry:随机池：抽中坏事件就在同池好事件里重抽")
            code = P.read_mem(h, base + e, 5) if e is not None else b""
            if code[:1] == b"\xe9":
                start = base + e + 5 + struct.unpack("<i", code[1:5])[0]
                stats = struct.unpack("<Q", P.read_mem(h, start - 8, 8))[0]
                vals = struct.unpack("<9Q", P.read_mem(h, stats, 72))
                log("事件统计(本次游戏启动以来，玩家的)：")
                for n, v in zip(EVSTATS_NAMES, vals):
                    log(f"  {n}: {v}")
            else:
                log("事件统计：补丁没装上")
            e = R.get("entry:全歼统计：消灭部队时还剩几支")
            code = P.read_mem(h, base + e, 5) if e is not None else b""
            if code[:1] == b"\xe9":
                start = base + e + 5 + struct.unpack("<i", code[1:5])[0]
                ws = struct.unpack("<Q", P.read_mem(h, start - 8, 8))[0]
                log("全歼统计(本次游戏启动以来)：")
                for n, v in zip(WIPESTATS_NAMES, struct.unpack("<6Q", P.read_mem(h, ws, 48))):
                    log(f"  {n}: {v}")
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
        spawn_watch(args.wait_game, args.restart_wait)
        log(f"已启动游戏: {game_cmd[0]}")
        return
    if args.watch:
        try:
            watch(args.wait_game, args.restart_wait)
        except Exception:
            log("守候进程出错退出:\n" + traceback.format_exc())
        return
    a = attach(args.wait_game)
    if not a:
        log("等待 eu4.exe 超时")
        sys.exit(1)
    h, base, img, pid = a
    res = install_all(h, base, img)
    log(f"补丁结果 (pid {pid}): " + "；".join(f"{n}:{r}" for n, r in res))


def spawn_watch(wait_game, restart_wait):
    """另起一个守候进程(不显示窗口)。"""
    import subprocess
    cmd = [sys.executable, str(SELF), "--watch", "--wait-game", str(wait_game), "--restart-wait", str(restart_wait)]
    # 守候进程在游戏退出后还会等一会儿：尽量脱离 Steam 的作业对象(CREATE_BREAKAWAY_FROM_JOB)，
    # 免得 Steam 一直显示游戏在运行；不允许脱离时照常启动
    try:
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x08000000 | 0x01000000, close_fds=True)
    except OSError:
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x08000000, close_fds=True)


class SelfUpdate:
    """守候进程运行期间脚本文件被改了：等文件写完(两次检查大小和时间都不变)且能编译，才算有新版本。"""

    def __init__(self):
        self.stamp = self._stamp()
        self.pending = None
        self.bad = None

    @staticmethod
    def _stamp():
        try:
            st = SELF.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def ready(self):
        cur = self._stamp()
        if cur is None or cur == self.stamp or cur == self.bad:
            self.pending = None
            return False
        if cur != self.pending:
            self.pending = cur
            return False
        try:
            compile(SELF.read_text(encoding="utf-8"), str(SELF), "exec")
        except Exception as e:
            log(f"脚本已修改但不能编译，继续用旧版本: {e!r}")
            self.bad = cur
            return False
        return True


def watch(wait_first, restart_wait):
    """守候：游戏在对局中返回主菜单等情况会自己重启 eu4.exe，新进程不经过 Steam 启动项，
    所以这里一直守着，每出现一个新的 eu4.exe 就装一次补丁；游戏退出后 restart_wait 秒内没再启动就结束。
    脚本文件被更新时：撤销本进程装的补丁，起一个新守候进程(用新脚本，重新装到正在运行的游戏上)，本进程退出。"""
    # 已有守候进程：可能是换版本时旧进程还没退出，也可能旧进程正处在“游戏退出后等它重启”的那一分钟里，
    # 而游戏是重新从 Steam 启动的(经过启动器，可能超过一分钟才出现 eu4.exe)。等旧进程结束再接手，
    # 旧进程已经接上游戏的话就一直等不到，到时退出即可。
    mutex = None
    deadline = time.time() + restart_wait + 30
    while True:
        mutex = P.single_instance(r"Local\eu4_buff_watch")
        if mutex is not None or time.time() > deadline:
            break
        time.sleep(0.5)
    if mutex is None:
        log("已有守候进程在运行，本进程退出")
        return
    upd = SelfUpdate()

    def handover(h=None, base=None, img=None):
        if h is not None:
            n = remove_all(h, base, img)
            P.k32.CloseHandle(h)
            log(f"脚本已更新：撤销旧补丁 {n} 处，换用新版本守候进程")
        else:
            log("脚本已更新：换用新版本守候进程")
        P.k32.CloseHandle(mutex)
        spawn_watch(wait_first, restart_wait)

    timeout = wait_first
    errors = 0
    while True:
        a = attach(timeout, upd.ready)
        if a == "stop":
            handover()
            return
        if not a:
            log("没有等到 eu4.exe 启动，守候结束")
            return
        h, base, img, pid = a
        try:
            res = install_all(h, base, img)
        except Exception:
            # 游戏刚启动时读写内存偶尔会失败：记下原因，过几秒对同一个进程再装(已装上的会跳过)
            errors += 1
            log(f"装补丁出错(第 {errors} 次，pid {pid}):\n" + traceback.format_exc())
            if errors >= 10:
                log("连续出错，守候结束")
                return
            time.sleep(3)
            timeout = 30
            continue
        errors = 0
        log(f"补丁结果 (pid {pid}): " + "；".join(f"{n}:{r}" for n, r in res))
        while not P.wait_exit(h, 2000):
            if upd.ready():
                handover(h, base, img)
                return
        log(f"eu4.exe (pid {pid}) 已退出，等待游戏重新启动")
        timeout = restart_wait


if __name__ == "__main__":
    main()
