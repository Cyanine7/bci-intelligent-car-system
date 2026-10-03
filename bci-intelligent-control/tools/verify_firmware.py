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
init_files = ['BALANCE/system.c', 'BALANCE/robot_select_init.c', 'HARDWARE/motor.c',
              'HARDWARE/encoder.c', 'HARDWARE/adc.c', 'HARDWARE/key.c',
              'HARDWARE/oled.c', 'HARDWARE/LED.C', 'USER/main.c',
              'USER/system_stm32f4xx.c', 'SYSTEM/delay/delay.c']
for filename in init_files:
    assert (root/filename).read_bytes().replace(b'\r\n',b'\n') == git_source(filename).replace(b'\r\n',b'\n'), filename

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
    'sha256':hashlib.sha256(image).hexdigest(), 'hardware_initialization_unchanged':True,
    'keil_build':'0 Error(s), 0 Warning(s)', 'hardware_flashed_in_this_task':False,
    'sources':{str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(root.rglob('*')) if p.suffix.lower() in ('.c','.h','.s','.uvprojx') and 'tests' not in p.parts}
}
out = root/'firmware_manifest.json'
out.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in manifest.items() if k!='sources'},ensure_ascii=False,indent=2))
