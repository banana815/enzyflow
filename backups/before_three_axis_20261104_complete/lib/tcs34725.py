# 文件路径：tcs34725.py
import time
from machine import I2C

class TCS34725:
    # 寄存器地址
    _REG_ENABLE = 0x00
    _REG_ATIME = 0x01
    _REG_CONTROL = 0x0F
    _REG_CDATA = 0x14
    _REG_RDATA = 0x16
    _REG_GDATA = 0x18
    _REG_BDATA = 0x1A
    _REG_ID = 0x12

    # 命令位，写寄存器时最高位必须为1
    _CMD_BIT = 0x80

    # 增益选项
    GAIN_1X = 0x00
    GAIN_4X = 0x01
    GAIN_16X = 0x02
    GAIN_60X = 0x03

    def __init__(self, i2c, addr=0x29):
        self.i2c = i2c
        self.addr = addr
        self.integration_time = 0xFF  # 2.4ms
        self.gain = self.GAIN_1X
        self._init_sensor()

    def _write_reg(self, reg, value):
        self.i2c.writeto_mem(self.addr, self._CMD_BIT | reg, bytes([value]))

    def _read_reg(self, reg, length=1):
        data = self.i2c.readfrom_mem(self.addr, self._CMD_BIT | reg, length)
        return data

    def _init_sensor(self):
        # 检查芯片ID
        chip_id = self._read_reg(self._REG_ID, 1)[0]
        #if chip_id != 0x44:
        if chip_id not in (0x44, 0x4D):
            raise RuntimeError('TCS34725 not found, chip id = 0x{:02X}'.format(chip_id))

        # 使能传感器
        self._write_reg(self._REG_ENABLE, 0x03)
        time.sleep_ms(10)
        # 设置积分时间
        self._write_reg(self._REG_ATIME, self.integration_time)
        # 设置增益
        self._write_reg(self._REG_CONTROL, self.gain)
        time.sleep_ms(10)

    def set_gain(self, gain):
        self.gain = gain
        self._write_reg(self._REG_CONTROL, gain)
        time.sleep_ms(10)

    def set_integration_time(self, atime):
        self.integration_time = atime
        self._write_reg(self._REG_ATIME, atime)
        time.sleep_ms(10)

    def read_raw(self):
        """读取原始 RGBC 数据，返回 (clear, red, green, blue)"""
        # 等待积分完成，简单起见使用固定延时
        # 实际可根据 ATIME 计算延时时间
        wait_time_ms = 2.4 + (256 - self.integration_time) * 2.4
        time.sleep_ms(int(wait_time_ms) + 1)

        data = self._read_reg(self._REG_CDATA, 8)
        clear = data[0] | (data[1] << 8)
        red = data[2] | (data[3] << 8)
        green = data[4] | (data[5] << 8)
        blue = data[6] | (data[7] << 8)
        return clear, red, green, blue

    def read_rgb(self):
        """读取标准 8 位 RGB 值，返回 (r, g, b)"""
        _, red, green, blue = self.read_raw()
        max_val = max(red, green, blue)
        if max_val == 0:
            return 0, 0, 0
        r = int(red * 255 / max_val)
        g = int(green * 255 / max_val)
        b = int(blue * 255 / max_val)
        return r, g, b

    def read_lux(self):
        """粗略计算光强，单位 Lux，仅供参考"""
        clear, red, green, blue = self.read_raw()
        lux = (clear - 1.87 * red - 0.65 * green - 0.36 * blue) / 1.0
        return max(lux, 0)
