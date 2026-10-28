"""Validate the actual Keil HEX/BIN, application partition and init preservation."""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
project = root / 'USER/WHEELTEC.uvprojx'
tree = ET.parse(project)
region = tree.find('.//OCR_RVCT4')
assert int(region.findtext('StartAddress'),16) == 0x08010000
assert int(region.findtext('Size'),16) == 0x70000
assert re.search(rb'#define\s+VECT_TAB_OFFSET\s+0x10000', (root/'USER/system_stm32f4xx.c').read_bytes())

image = (root/'WHEELTEC.bin').read_bytes()
assert 8 <= len(image) <= 0x70000
sp, reset = struct.unpack_from('<II', image)
assert 0x20000000 <= sp <= 0x20020000 and sp % 8 == 0
assert reset & 1 and 0x08010000 <= (reset & ~1) < 0x08010000 + len(image)
memory = {}
upper = 0
for line in (root/'OBJ/WHEELTEC.hex').read_text().splitlines():
    assert line.startswith(':')
    record = bytes.fromhex(line[1:])
    assert sum(record) & 255 == 0
    count, hi, lo, kind = record[:4]
    assert len(record) == count + 5
    if kind == 4:
        upper = int.from_bytes(record[4:6], 'big') << 16
    elif kind == 0:
        for offset, byte in enumerate(record[4:-1]):
            address = upper + (hi << 8) + lo + offset
            assert 0x08010000 <= address < 0x08080000
            memory[address] = byte
assert min(memory) == 0x08010000
assert max(memory) == 0x08010000 + len(image) - 1
for address, byte in memory.items():
    assert image[address - 0x08010000] == byte

def git_source(path):
    return subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=root)

# All hardware initialization files other than USART2's runtime handler are
# unchanged. Preserve exact bytes; source remains the original GBK format.
init_files = ['BALANCE/robot_select_init.c', 'HARDWARE/motor.c',
              'HARDWARE/encoder.c', 'HARDWARE/adc.c', 'HARDWARE/key.c',
              'HARDWARE/oled.c', 'HARDWARE/LED.C',
              'USER/system_stm32f4xx.c', 'SYSTEM/delay/delay.c']
for filename in init_files:
    assert (root/filename).read_bytes().replace(b'\r\n',b'\n') == git_source(filename).replace(b'\r\n',b'\n'), filename

# The IMU port adds exactly one init call after all existing hardware setup and
# one task. Preserve every original init argument and original task otherwise.
imu_init_addition = (b'    /* Initialize the IMU after existing hardware setup, before scheduling. */\n'
                     b'    ICM20948_DMP_Init();\n')
expected_system = git_source('BALANCE/system.c').replace(b'\r\n', b'\n')
expected_system = expected_system.replace(b'\t \t\n}', b'\t \t\n' + imu_init_addition + b'}', 1)
assert (root/'BALANCE/system.c').read_bytes().replace(b'\r\n',b'\n') == expected_system
expected_main = git_source('USER/main.c').replace(b'\r\n',b'\n')
expected_main = expected_main.replace(b'\t  xTaskCreate(Balance_task,',
    b'\txTaskCreate(sensor_task,  "sensor_task",  SENSOR_STK_SIZE,  NULL, SENSOR_TASK_PRIO,  NULL);\n'
    b'\t  xTaskCreate(Balance_task,', 1)
assert (root/'USER/main.c').read_bytes().replace(b'\r\n',b'\n') == expected_main
groups = {g.findtext('GroupName'): [f.findtext('FilePath') for f in g.findall('Files/File')]
          for g in tree.findall('.//Group')}
assert groups['IMU'] == ['..\\IMU\\' + n for n in ('bsp_siic.c', 'imu.c', 'icm20948_dmp.c', 'icm20948.c')]
assert '..\\BALANCE\\sensor.c' in groups['Balance']
for speed_file in ('speed_control.c', 'speed_capture.c', 'speed_experiment.c', 'yaw_control.c', 'yaw_experiment.c'):
    assert '..\\BALANCE\\' + speed_file in groups['Balance']
assert '..\\IMU' in tree.findtext('.//Cads/VariousControls/IncludePath').split(';')
sensor_source = (root/'BALANCE/sensor.c').read_text(encoding='utf-8')
assert 'Read_Encoder(' not in sensor_source and 'Get_Velocity_From_Encoder(' not in sensor_source
sensor_priority = int(re.search(r'#define\s+SENSOR_TASK_PRIO\s+(\d+)', (root/'BALANCE/sensor.h').read_text(encoding='utf-8'))[1])
control_priority = int(re.search(rb'#define\s+BALANCE_TASK_PRIO\s+(\d+)', (root/'BALANCE/balance.h').read_bytes())[1])
show_priority = int(re.search(rb'#define\s+SHOW_TASK_PRIO\s+(\d+)', (root/'BALANCE/show.h').read_bytes())[1])
led_priority = int(re.search(rb'#define\s+LED_TASK_PRIO\s+(\d+)', (root/'HARDWARE/LED.H').read_bytes())[1])
assert (control_priority, sensor_priority, show_priority, led_priority) == (4, 3, 2, 1)
assert sensor_source.index('ICM20948_ReadGyroZNow(') < sensor_source.index('snapshot.gyro_tick_ms =') < sensor_source.index('ICM20948_DMP_Update(')
assert b'#include "icm20948_img.dmp3a.h"' not in (root/'BALANCE/system.h').read_bytes()

# User-authorized geometry update, separate from unchanged init flow.
geometry = (root/'BALANCE/robot_select_init.h').read_bytes().replace(b'\r\n',b'\n')
expected_geometry = git_source('BALANCE/robot_select_init.h').replace(b'\r\n',b'\n')
expected_geometry = expected_geometry.replace(b'Diff_wheelSpacing        0.177f',
                                             b'Diff_wheelSpacing        0.160f')
expected_geometry = expected_geometry.replace(b'Great_WheelDiameter   0.085',
                                             b'Great_WheelDiameter   0.066')
assert geometry == expected_geometry, 'Only measured differential-drive geometry may change'

def uart_init_slice(data):
    start = data.index(b'void uart1_init(')
    end = data.index(b'int USART1_IRQHandler(')
    return data[start:end].replace(b'\r\n', b'\n')
assert uart_init_slice((root/'HARDWARE/usartx.c').read_bytes()) == uart_init_slice(git_source('HARDWARE/usartx.c'))
log = (root/'USER/keil_build.log').read_text(errors='replace')
assert '0 Error(s), 0 Warning(s)' in log
assert (root/'WHEELTEC.bin').stat().st_mtime >= (root/'OBJ/WHEELTEC.axf').stat().st_mtime
manifest = {
    'protocol':'PROJECT_V1', 'application_address':'0x08010000',
    'application_partition_size':448*1024, 'vector_offset':'0x10000',
    'size_bytes':len(image), 'initial_sp':f'0x{sp:08X}', 'reset_handler':f'0x{reset:08X}',
    'sha256':hashlib.sha256(image).hexdigest(), 'hardware_initialization_unchanged':False,
    'existing_hardware_initialization_preserved':True,
    'imu_initialization_added':True, 'imu_task_poll_hz':100,
    'task_priorities':dict(control=control_priority, imu=sensor_priority, show=show_priority, led=led_priority),
    'gyro_published_before_dmp':True,
    'imu_attitude_transport':'OLED; PROJECT_V1 yaw extension transports independent gyro samples',
    'mechanical_geometry_updated':True, 'wheel_diameter_m':0.066,
    'wheel_center_spacing_m':0.160, 'encoder_counts_per_wheel_turn_nominal':1560,
    'keil_build':'0 Error(s), 0 Warning(s)', 'hardware_flashed_in_this_task':False,
    'speed_experiment_version':2, 'speed_gain_format':'IEEE754 binary64',
    'speed_algorithm_implemented':bool(int(re.search(r'#define\s+SPEED_CONTROL_IMPLEMENTED\s+(\d+)',
        (root/'BALANCE/speed_control.h').read_text(encoding='utf-8'))[1])),
    'speed_target_prefilter':bool(int(re.search(r'#define\s+SPEED_CONTROL_TARGET_FILTER\s+(\d+)',
        (root/'BALANCE/speed_control.h').read_text(encoding='utf-8'))[1])),
    'speed_target_prefilter_tau':'Kp/Ki',
    'yaw_experiment_version':1, 'yaw_max_segment_ms':5000, 'yaw_max_total_ms':10000,
    'yaw_default_segment_ms':2000, 'yaw_capture_capacity':1040, 'yaw_sample_bytes':84,
    'yaw_gyro_stale_ms':60, 'experiment_storage_shared':True,
    'speed_pwm_limit':16500, 'speed_max_total_ms':3000, 'speed_capture_capacity':384,
    'sources':{str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(root.rglob('*')) if p.suffix.lower() in ('.c','.h','.s','.uvprojx') and 'tests' not in p.parts}
}
out = root/'firmware_manifest.json'
out.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in manifest.items() if k!='sources'},ensure_ascii=False,indent=2))
