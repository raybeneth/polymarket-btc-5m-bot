"""打印 py_clob_client_v2 的 SignatureTypeV2 枚举成员及其数值。

在装有 py-clob-client-v2 的环境（容器/服务器）运行：
    python3 print_sigtypes.py
"""
from py_clob_client_v2 import SignatureTypeV2

for t in SignatureTypeV2:
    print(t, "->", t.value)
