"""
csv_logger.py
=============
CSV 数据落盘模块 + 数值格式化辅助。

职责:
    把原 main_app.py 中分散的三类功能集中维护:
        1) 数值格式化  (_round_fmt / _round3 / _round4, 原 41-51)
        2) 数据目录生成 (make_dated_folder, 原 381-393)
        3) CSV 文件生命周期 (CSVLogger 类, 原 134、398-410、562-568、599-605)

    输出文件保持与原版逐字节一致:
        - UTF-8 BOM (\\xef\\xbb\\xbf) 开头
        - 头 4 行 (通道、采样频率、采样时间、列标题), CRLF 换行, 全角冒号
        - 数据行 5 列空格分隔, 去尾零格式化, CRLF 结尾
        - 每行写入后立刻 flush

依赖:
    - public_para.g (本模块只读, 不写)
    - 标准库 os / datetime

被调用方:
    - main_app.MainForm._btn_start_click → make_dated_folder + CSVLogger.open
    - main_app.MainForm._on_data_ready   → CSVLogger.write_row
    - main_app.MainForm._btn_fit_click   → CSVLogger.append_fit (追加拟合函数)
    - main_app.MainForm._on_timer1       → CSVLogger.close
    - main_app.MainForm.closeEvent       → CSVLogger.close
"""

import os
from datetime import datetime

from public_para import g  # noqa: F401  (保留导入, 便于将来从此处读 g 字段)


# =============================================================================
# 数值格式化 (与原 main_app.py:41-51 等价)
# =============================================================================
def _round_fmt(val: float, decimals: int) -> str:
    """把浮点数按固定小数位格式化, 再去掉末尾的 0 和 '.'。

    Args:
        val:      待格式化的浮点数
        decimals: 保留小数位数 (≥ 0)

    Returns:
        格式化后的字符串; 当结果为空或仅 '-' 时返回 '0'

    Side Effects:
        无 (纯函数)

    行为不变量 (与原 main_app.py:41-48 一致, 等价于 Delphi FloatToStr+RoundTo):
        - 先用 ('%.<N>f') % round(val, N) 生成固定小数位
        - 含 '.' 时先 rstrip('0') 再 rstrip('.')
        - 空串或 '-' 视为 0
    """
    s = ('%.' + str(decimals) + 'f') % round(val, decimals)
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    if not s or s == '-':
        return '0'
    return s


def _round3(val: float) -> str:
    """把浮点数格式化为最多 3 位小数 (去尾零)。

    Args:
        val: 待格式化的浮点数

    Returns:
        格式化字符串

    Side Effects:
        无
    """
    return _round_fmt(val, 3)


def _round4(val: float) -> str:
    """把浮点数格式化为最多 4 位小数 (去尾零)。

    Args:
        val: 待格式化的浮点数

    Returns:
        格式化字符串

    Side Effects:
        无
    """
    return _round_fmt(val, 4)


# =============================================================================
# 数据目录生成
# =============================================================================
def make_dated_folder(exe_dir: str):
    """在 <exe_dir>/data 下创建当日不冲突的子目录, 命名为 YYYYMMDDxx。

    Args:
        exe_dir: 可执行程序所在目录绝对路径 (由调用方传入, 通常是 MainForm.exe_directory)

    Returns:
        (full_path, test_no) 二元组:
            - full_path: 新建目录的绝对路径
            - test_no:   目录名 (如 '2026051803'), 同时用于 CSV / PNG 文件名前缀

    Side Effects:
        - 在磁盘上创建目录 (os.makedirs)
        - 若当日的 01 ~ N-1 序号目录已存在, 自动递增到首个未占用的序号

    行为不变量 (与原 main_app.py:381-393 一致):
        - date_str 用 '%Y%m%d' 格式
        - 子目录序号 i 从 1 起, 格式化为两位数 '01'..'99'
        - 当 i ≥ 100 时格式 '{i:02d}' 会输出三位; 原代码同样不做特殊处理 → 保持兼容
    """
    now = datetime.now()
    date_str = now.strftime('%Y%m%d')
    base = os.path.join(exe_dir, 'data')
    i = 1
    s1 = '01'
    sub = f'{date_str}{s1}'
    full = os.path.join(base, sub)
    while os.path.exists(full):
        i += 1
        s1 = f'{i:02d}'
        sub = f'{date_str}{s1}'
        full = os.path.join(base, sub)
    os.makedirs(full, exist_ok=True)
    test_no = f'{date_str}{s1}'
    return (full, test_no)


# =============================================================================
# CSVLogger 类: 封装 CSV 文件的生命周期
# =============================================================================
class CSVLogger:
    """封装 CSV 文件的 open / write_row / close, 保持与原版完全一致的字节格式。

    职责:
        - 维护单个二进制写入文件句柄
        - 在 open() 时写入 BOM + 4 行头信息
        - 在 write_row() 时写入 1 行 5 列数据 (空格分隔, CRLF 结尾)
        - 在 close() 时安全关闭句柄, 并提供 is_open / path 状态查询

    典型用法:
        csv = CSVLogger()
        csv.open(full_dir, test_no, 10000, 2000)
        csv.write_row(elapsed, dis0, dis2, deformation, realspeed)
        ...
        csv.close()

    维护的状态:
        - self.file: 文件句柄 (bytes 模式), 关闭后置 None
        - self.path: 文件绝对路径 (open 后赋值)
    """

    def __init__(self):
        """初始化空 logger (尚未打开任何文件)。

        Args:
            无

        Returns:
            无

        Side Effects:
            无 (不创建文件)
        """
        self.file = None
        self.path = ''

    def open(self, full_dir: str, test_no: str, sample_rate: int, savetime: int,
             channel_name: str = '通道1',
             columns: str = '时间 初始间距 当前间距 变形量 转速',
             file_suffix: str = '') -> str:
        """打开 CSV 文件并写入 BOM + 4 行头信息。

        Args:
            full_dir:    数据目录绝对路径 (一般是 make_dated_folder 的返回)
            test_no:     文件名前缀 (一般同目录名, 如 '2026051803')
            sample_rate: 采样频率 (Hz), 整型, 用于写到头第 2 行
            savetime:    采集时长 (秒), 整型, 用于写到头第 3 行
            channel_name: 头第 1 行的通道名 (振动页用所选振动通道文本;
                          默认 '通道1' 与径向页历史行为一致)
            columns:     头第 4 行的列标题 (振动页为 6 列含"振幅"; 默认 5 列)
            file_suffix: 文件名后缀 (共享采集会话下振动页用 '_vib' 与径向
                         CSV 同目录共存; 默认空 = 历史行为)

        Returns:
            打开的 CSV 文件绝对路径 (同 self.path)

        Side Effects:
            - 创建并打开 <full_dir>/<test_no><file_suffix>.CSV 写入文件 (二进制 'wb')
            - 写入 BOM 与 4 行头信息
            - self.file / self.path 被赋值

        行为不变量 (默认参数下与原 main_app.py:396-410 字节一致):
            BOM 为 b'\\xef\\xbb\\xbf';
            头 4 行依次:
                f"{channel_name}\\r\\n"
                f"采样频率：{sample_rate}\\r\\n"   (全角冒号)
                f"采样时间：{savetime}s\\r\\n"    (全角冒号)
                f"{columns}\\r\\n"
        """
        self.path = os.path.join(full_dir, f'{test_no}{file_suffix}.CSV')
        self.file = open(self.path, 'wb')
        # UTF-8 BOM, 与原版一致, 便于 Excel / VSCode 自动识别编码
        self.file.write(b'\xef\xbb\xbf')
        # 头 4 行: UTF-8 编码, CRLF 行尾, 全角冒号
        self.file.write(f"{channel_name}\r\n".encode('utf-8'))
        self.file.write(f"采样频率：{sample_rate}\r\n".encode('utf-8'))
        self.file.write(f"采样时间：{savetime}s\r\n".encode('utf-8'))
        self.file.write(f"{columns}\r\n".encode('utf-8'))
        return self.path

    def write_row(self, elapsed: float, dis0: float, dis2: float,
                  deformation: float, realspeed: float) -> None:
        """写入一行数据 (5 列空格分隔, CRLF 结尾), 并立即 flush。

        Args:
            elapsed:     自采集开始累计的秒数, 写出时最多 3 位小数 (去尾零)
            dis0:        初始间距 g.Dis0  (μm), 最多 4 位小数
            dis2:        当前间距 g.Dis2  (μm), 最多 4 位小数
            deformation: 当前变形量      (μm), 最多 4 位小数
            realspeed:   实时转速        (RPM), 最多 4 位小数

        Returns:
            无

        Side Effects:
            - 写入一行到 self.file
            - 调用 flush() 立刻持久化 (与原 main_app.py:568 一致, 防止断电丢数据)
            - 若 self.file 为 None (尚未 open 或已 close), 静默忽略

        行为不变量:
            - 列之间是单个空格, 行尾固定 '\\r\\n'
            - 数值格式: elapsed 用 _round3, 其余 4 列用 _round4
            - 列顺序: 时间 / 初始间距 / 当前间距 / 变形量 / 转速 (与表头一致)
        """
        if self.file is None:
            return
        line = (
            f"{_round3(elapsed)} "
            f"{_round4(dis0)} "
            f"{_round4(dis2)} "
            f"{_round4(deformation)} "
            f"{_round4(realspeed)}\r\n"
        )
        self.file.write(line.encode('utf-8'))
        self.file.flush()

    def write_row6(self, elapsed: float, dis0: float, dis2: float,
                   deformation: float, vib1: float, realspeed: float) -> None:
        """写入振动页的一行 6 列数据 (空格分隔, CRLF 结尾), 并立即 flush。

        Args:
            elapsed:     自采集开始累计的秒数, 最多 3 位小数 (去尾零)
            dis0:        初始间距 (μm), 最多 4 位小数
            dis2:        当前间距 (μm), 最多 4 位小数
            deformation: 变形量 (μm), 最多 4 位小数
            vib1:        振动峰峰值"振幅" (μm), 最多 4 位小数
            realspeed:   实时转速 (RPM), 最多 4 位小数

        Returns:
            无

        Side Effects:
            - 写入一行到 self.file 并 flush; 文件未打开时静默忽略

        行为不变量 (对照 Delphi Main.pas 保存行):
            - 列顺序: 时间 / 初始间距 / 当前间距 / 变形量 / 振幅 / 转速
              (与 open(columns=...) 传入的 6 列表头一致)
            - 数值格式与 write_row 相同 (_round3 / _round4 去尾零)
        """
        if self.file is None:
            return
        line = (
            f"{_round3(elapsed)} "
            f"{_round4(dis0)} "
            f"{_round4(dis2)} "
            f"{_round4(deformation)} "
            f"{_round4(vib1)} "
            f"{_round4(realspeed)}\r\n"
        )
        self.file.write(line.encode('utf-8'))
        self.file.flush()

    def append_fit(self, lines) -> str:
        """把拟合函数等文本追加到本次采集已保存的 CSV 末尾 (UTF-8, CRLF)。

        Args:
            lines: 字符串列表, 每个元素写为一行 (自动补 CRLF)

        Returns:
            实际追加到的文件绝对路径; 若无有效 path 或文件不存在, 返回 ''

        Side Effects:
            - 以二进制追加模式 ('ab') 打开 self.path 写入若干行 (不改动已写入的
              采集数据, 仅在末尾追加)
            - 失败时吞异常并返回 ''

        说明:
            供拟合完成后调用, 把分段幂函数与确定系数写进该次采集的数据文件;
            可在文件已 close 之后调用 (用 self.path 重新追加)。
        """
        if not self.path or not os.path.exists(self.path):
            return ''
        try:
            with open(self.path, 'ab') as f:
                for ln in lines:
                    f.write((str(ln) + '\r\n').encode('utf-8'))
            return self.path
        except Exception:
            return ''

    def close(self) -> None:
        """安全关闭文件句柄, 多次调用幂等。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 若 self.file 已打开, 调用 close() 并置 None
            - 关闭过程中的异常被吞掉 (与原 main_app.py:602-604 / 624-625 一致),
              避免在程序退出 / 定时器回调中抛错
        """
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None

    @property
    def is_open(self) -> bool:
        """文件是否已打开。

        Args:
            无

        Returns:
            True: self.file 非 None; False: 尚未 open 或已 close

        Side Effects:
            无
        """
        return self.file is not None
