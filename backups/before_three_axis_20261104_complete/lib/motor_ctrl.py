import struct
import time

class MotorController:
    def __init__(self, uart, addr=0x01):
        """
        初始化电机控制类
        :param uart: 已初始化的 UART 实例
        :param addr: 电机从机地址，0~255，默认为 0x01 (0为广播地址)
        """
        self.uart = uart
        self.addr = addr

    def _calc_checksum(self, data):
        """内部方法：计算校验和，等于所有前置字节之和的低8位"""
        return sum(data) & 0xFF
    
    def rec_chuli(self, data):
        # 初始化标准返回结构
        result = {"id": 1, "angle":0.0}

        if len(data)    >= 10 and data[0] == 0xC5 and data[2] == 0x2A:
            motor_angle   = struct.unpack('>i', data[4:8])[0]
            motor_angle   = round((motor_angle / 51200.0 ) * 360.0)
            
            self.addr = data[1]
            result["id"] = data[1]
            result["angle"] = motor_angle
            print(f"电机当前机号: 0x{result['id']:02X}, 角度: {motor_angle}")
            return result
    
        # 校验应答帧：帧头0xC5，地址匹配，功能码0x20[cite: 1]
        if len(data)    >= 7 and data[0] == 0xC5 and data[2] == 0x20:
            self.addr = data[1]
            print(f"成功找到电机，当前机号为: 0x{self.addr:02X}")
            
            result["id"] = self.addr
            return result 
        
        # 校验返回帧结构：帧头0xC5，原地址，指令0x60[cite: 1]
        if len(data)    >= 7 and data[0] == 0xC5 and data[2] == 0x60:
        # 提取 Byte1(操作结果) 和 Byte2(新的从机地址)[cite: 1]
            if data[3] == 0x01 :
                self.addr = data[4]
                print(f"机号修改成功！新机号: {self.addr}")
                
                result["id"] = self.addr
                return result 
            
        return result
            
    def scan_motor_id(self, start_id=1, end_id=10):
        """
        扫描并获取当前总线上的电机机号
        原理：因为广播地址(0)不会产生应答，所以通过轮询读取硬件版本指令(0x20)来寻找电机[cite: 1]
        :return: 扫描到的第一个电机 ID，如果没找到则返回 None
        """
        print(f"开始扫描电机ID (范围 {start_id}~{end_id})...")
  
        for test_id in range(start_id, end_id + 1):
            # 构造 0x20 读取软硬件版本指令作为 Ping 信号[cite: 1]
            cmd = [0xC5, test_id, 0x20]
            cmd.append(self._calc_checksum(cmd))
            cmd.append(0x5C)
            self.uart.write(bytes(cmd))
            time.sleep(0.05)
            
    def set_motor_id(self, new_id):
        """
        配置(修改)电机机号
        :param new_id: 新的电机地址 (1~255)
        :return: bool, 修改是否成功
        """
        if not (1 <= new_id <= 255):
            print("错误：机号必须在 1~255 之间")
            return False

        # 构造 0x60 设置从机地址指令[cite: 1]
        cmd = [0xC5, self.addr, 0x60, new_id]
        cmd.append(self._calc_checksum(cmd))
        cmd.append(0x5C)     
        self.uart.write(bytes(cmd))
        time.sleep(0.05)
    
    def rotate_motor_angle(self, id_num, angle_deg, speed_rps=0.2, acc=100):
        """
        按指定速度旋转指定角度
        :param angle_deg: 旋转角度，单位：度（正数正转，负数反转）
        :param speed_rps: 旋转速度，单位：圈/秒（默认 0.2 圈/秒）
        :param acc: 加减速度，取值范围 0~200（默认 100）
        """
        # 将圈/秒转换为 RPM (转/分钟)[cite: 1]
        speed_rpm = int(speed_rps * 60)
        # 将角度转换为脉冲数：每圈对应 51200 个脉冲[cite: 1]
        pulses = int((abs(angle_deg) / 360.0) * 51200)
        # 确定方向：0 为正转，1 为反转[cite: 1]
        direction = 0 if angle_deg >= 0 else 1
        
        # 构造 0xF3 相对位置模式控制指令[cite: 1]
        cmd = [
            0xC5, id_num, 0xF3, 
            direction, 
            acc, 
            (speed_rpm >> 8) & 0xFF, speed_rpm & 0xFF,
            (pulses >> 24) & 0xFF, (pulses >> 16) & 0xFF, (pulses >> 8) & 0xFF, pulses & 0xFF
        ]
        
        # 增加校验码和帧尾 (0x5C)[cite: 1]
        cmd.append(self._calc_checksum(cmd))
        cmd.append(0x5C)
        
        self.uart.write(bytes(cmd))
        time.sleep(0.05)

    def get_realtime_angle(self):
        """
        实时获取电机当前旋转角度
        :return: 当前角度值(浮点数度数)，若超时未读到则返回 None
        """
        # 构造 0x2A 读取电机实时位置指令[cite: 1]
        cmd = [0xC5, self.addr, 0x2A]
        cmd.append(self._calc_checksum(cmd))
        cmd.append(0x5C)
        
        self.uart.write(bytes(cmd))
                
        time.sleep(0.05)

    def clear_motor_angle(self):
        """
        将当前的位置角度清零，即设定当前位置为 0 度
        """
        # 构造 0xF8 将当前位置角度清零指令[cite: 1]
        cmd = [0xC5, self.addr, 0xF8]
        cmd.append(self._calc_checksum(cmd))
        cmd.append(0x5C)
        
        self.uart.write(bytes(cmd))
        time.sleep(0.05)