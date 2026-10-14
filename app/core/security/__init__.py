"""密钥加密：api key **只以密文入库**。

Windows 上用 DPAPI（``CryptProtectData`` / ``CryptUnprotectData``）：密文由系统按**当前用户**
保管，我们既不用自己管一个密钥文件，也不会掉进"密钥和密文放在一起"的自欺欺人里。

代价要说清：密文只在**同一台机器的同一个用户**下能解开（换机器 / 换用户 = 解不开）——
对一个"数据都在本机"的桌面应用来说，这正是想要的性质。

对外只有两个函数：:func:`protect` / :func:`unprotect`。
"""

from app.core.security.dpapi import SecurityError, protect, unprotect

__all__ = ["SecurityError", "protect", "unprotect"]
