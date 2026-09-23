# 阶段 3 实施 SPEC：进程环境继承策略

本文件只包含可执行内容。设计论证、实测数据、对标分析见 `Runtime安全信任与Capability Sandbox架构演进SPEC-V1.md` 第 8 节「第三阶段」。

---

## 1. 目标

子进程当前只能拿到两个硬编码环境变量，导致 `git push` 因缺少 `SSH_AUTH_SOCK` 而失败。

改为按启动时确定的策略组装环境，默认档 `core` 使常规开发命令零配置可用。

**验收标准**：不修改任何配置的情况下，Agent 执行 `git push` 成功。

---

## 2. 改动清单

| 序号 | 文件 | 改动 |
|---|---|---|
| 2.1 | `src/tsm_agt/bootstrap/process_environment_configuration.py` | 新建 |
| 2.2 | `src/tsm_agt/adapters/local_process/executor.py` | `__init__` 接收策略；重写 `prepare_environment` |
| 2.3 | `src/tsm_agt/adapters/windows_process/executor.py` | 同上 |
| 2.4 | `src/tsm_agt/bootstrap/composition.py` | 加载配置并传入 executor 构造 |
| 2.5 | `src/tsm_agt/adapters/local_sandbox/sandbox.py` | 放行策略已授权的变量名 |
| 2.6 | `src/tsm_agt/adapters/local_sandbox/sandbox.py` | trust cache 写入容错 |

**不改动**：`ProcessExecutorPort` 契约、`Kernel`、`ProcessStartRequest`、`SandboxRequest`。

策略通过 executor 构造函数注入，不改 `prepare_environment` 签名。理由：策略是启动期固定值，构造注入语义正确，且避免 Port 契约变更带来的测试面扩散。

---

## 3. 新建：bootstrap 配置模块

文件：`src/tsm_agt/bootstrap/process_environment_configuration.py`

严格照 `egress_configuration.py` 的结构实现：模块级 `*_ENV_NAMES` 元组、frozen dataclass、`load_*` 函数、复用 `_selected` / `_read_values` 的同形实现（导出值优先于 `.env` 文件值，均为空时取默认）。

### 3.1 常量

```python
PROCESS_ENV_NAMES = (
    "AGT_PROCESS_ENV_INHERIT",
    "AGT_PROCESS_ENV_INCLUDE",
    "AGT_PROCESS_ENV_SET",
)

CORE_ENV_NAMES = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSH_AUTH_SOCK",
)

FALLBACK_ENV_VALUES = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}
```

`CORE_ENV_NAMES` 是代码常量，不是配置项。

`FALLBACK_ENV_VALUES` 即当前硬编码值，仅在 `inherit=none` 时使用。

### 3.2 数据结构

```python
class ProcessEnvInherit(StrEnum):
    CORE = "core"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ProcessEnvironmentConfiguration:
    inherit: ProcessEnvInherit = ProcessEnvInherit.CORE
    include: tuple[str, ...] = ()
    set_values: Mapping[str, str] = field(default_factory=dict)
    sources: Mapping[str, str] | None = None

    @property
    def inherited_names(self) -> tuple[str, ...]:
        """按声明顺序去重后的待继承变量名。"""
        if self.inherit is ProcessEnvInherit.CORE:
            return tuple(dict.fromkeys(CORE_ENV_NAMES + self.include))
        return tuple(dict.fromkeys(self.include))

    @property
    def fallback_values(self) -> Mapping[str, str]:
        """inherit=none 时的固定基线。core 档不使用。"""
        if self.inherit is ProcessEnvInherit.NONE:
            return dict(FALLBACK_ENV_VALUES)
        return {}
```

### 3.3 解析规则

`AGT_PROCESS_ENV_INHERIT`

- 取值 `core` / `none`，大小写不敏感
- 默认 `core`
- 其他值抛 `ValueError`，文案格式对齐 `egress_configuration.py`：`AGT_PROCESS_ENV_INHERIT must be 'core' or 'none'`
- **不接受 `all`**，须与非法值同样报错

`AGT_PROCESS_ENV_INCLUDE`

- 逗号分隔变量名，如 `KUBECONFIG,DOCKER_HOST`
- 默认空
- 逐项 strip 后丢弃空串
- 名字含 `=` 或 NUL 时抛 `ValueError`
- 不校验宿主是否存在该变量（组装阶段静默跳过）

`AGT_PROCESS_ENV_SET`

- 逗号分隔 `KEY=VALUE`
- 默认空
- 无 `=` 的条目抛 `ValueError`
- 只按首个 `=` 切分，值中允许含 `=`
- key 为空时抛 `ValueError`

`sources` 字段记录每项来源（`environment` / `env_file` / `default`），与现有配置模块一致。

---

## 4. 改造：POSIX executor

文件：`src/tsm_agt/adapters/local_process/executor.py`

### 4.1 构造函数

```python
def __init__(
    self,
    environment_policy: ProcessEnvironmentConfiguration | None = None,
) -> None:
    self._started = False
    self._live: dict[str, _LiveProcess] = {}
    self._completed: dict[...] = {}
    self._environment_policy = (
        environment_policy or ProcessEnvironmentConfiguration()
    )
```

默认值保证现有直接实例化 `LocalProcessExecutor()` 的测试不报错（行为会从「两个硬编码变量」变为 `core` 档，见 7.3）。

### 4.2 `prepare_environment`

改造前（`executor.py:107`）：

```python
def prepare_environment(self, environment) -> dict[str, str]:
    safe = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
    }
    safe.update(environment)
    return safe
```

改造后：

```python
def prepare_environment(self, environment) -> dict[str, str]:
    policy = self._environment_policy
    safe = dict(policy.fallback_values)
    for name in policy.inherited_names:
        value = os.environ.get(name)
        if value is not None:
            safe[name] = value
    safe.update(policy.set_values)
    safe.update(environment)
    return safe
```

优先级由低到高：`fallback_values` → 继承值 → `set_values` → 调用方显式传参。

宿主不存在的变量静默跳过，不报错。

### 4.3 暴露策略供诊断使用

```python
@property
def environment_policy(self) -> ProcessEnvironmentConfiguration:
    return self._environment_policy
```

供后续诊断输出读取，本批不接线。

---

## 5. 改造：Windows executor

文件：`src/tsm_agt/adapters/windows_process/executor.py`

现有实现（`executor.py:97`）已在继承宿主变量，只是清单硬编码：

```python
required = ("SystemRoot", "ComSpec", "PATHEXT", "TEMP", "TMP", "PATH")
```

改造要求：

1. `__init__` 同样接收 `environment_policy`
2. 保留上述 6 个 Windows 必需变量作为**平台基线，始终继承**，与 `inherit` 档位无关——缺失会导致进程无法启动
3. 在平台基线之上叠加 `policy.inherited_names`
4. 保持现有 casefold 去重逻辑（Windows 环境变量名大小写不敏感）
5. `policy.set_values` 与调用方 `environment` 的叠加顺序与 POSIX 一致

平台基线与 `CORE_ENV_NAMES` 的交集（`PATH`）由 casefold 去重自然处理。

---

## 6. 改造：composition 装配

文件：`src/tsm_agt/bootstrap/composition.py`

### 6.1 加载配置

在两处 `load_*_configuration` 调用点（约 1025 行、1067 行）旁增加：

```python
process_environment = load_process_environment_configuration(
    env_file or Path.cwd() / ".env", os.environ
)
```

并作为命名参数传入对应的 compose 函数，形式对齐 `web_egress_configuration=egress`。

### 6.2 传入 executor

`_platform_process_executor()`（约 410 行）增加参数：

```python
def _platform_process_executor(
    environment_policy: ProcessEnvironmentConfiguration | None = None,
) -> ProcessExecutorPort:
    if os.name == "nt":
        return WindowsProcessExecutor(environment_policy)
    return LocalProcessExecutor(environment_policy)
```

三处调用点（约 521、768、915 行）传入策略。其中 521 行处保留 `process_adapter or ...` 的现有覆盖语义。

### 6.3 compose 函数签名

两个 compose 函数各增加一个可选参数，与现有可选配置参数写法一致：

```python
process_environment_configuration: ProcessEnvironmentConfiguration | None = None,
```

函数体内：

```python
process_environment = (
    process_environment_configuration or ProcessEnvironmentConfiguration()
)
```

---

## 7. 改造：sandbox

文件：`src/tsm_agt/adapters/local_sandbox/sandbox.py`

### 7.1 放行已授权变量

现状（第 117 行起）对整个 environment 做凭据名检查：

```python
for name, value in request.environment.items():
    lowered = name.lower()
    if (
        not name or "=" in name or "\x00" in name or "\x00" in value
        or any(fragment in lowered for fragment in _CREDENTIAL_FRAGMENTS)
    ):
        return SandboxDecision(False, "credential-like or invalid environment entry is denied")
```

问题：`prepare_environment` 先执行、sandbox 后审查，注入的变量会进入 `SandboxRequest.environment`。一旦用户配置 `AGT_PROCESS_ENV_INCLUDE=GITHUB_TOKEN`，会被自身 sandbox 拒绝。

改造：构造时接收一份「已授权名字集合」（即 `policy.inherited_names` 与 `set_values` 键的并集），命中该集合的名字跳过 `_CREDENTIAL_FRAGMENTS` 检查。

**格式校验（空名、含 `=`、含 NUL）对所有变量一律保留**，不得跳过。

若在 `SandboxDecision.reason` 或 `diagnostics` 中输出被拦名字，可写变量名，**不得写变量值**。

### 7.2 trust cache 写入容错

现状（第 319 行）：

```python
def _store_runtime_trust_cache(self, workspace_root, cache) -> None:
    cache_path = workspace_root / _RUNTIME_TRUST_CACHE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
```

未捕获异常。只读挂载、磁盘写满、`.tsm/` 权限异常时 `OSError` 会穿透 `authorize`，表现为无法解释的执行失败。

改造：捕获 `OSError`，返回持久化是否成功的标志，由调用方将失败原因写入 `SandboxDecision.diagnostics`（建议键名 `trust_cache_persisted` / `trust_cache_error`）。

**决策本身仍然生效**，只是不持久化。不得让异常穿透授权路径。

### 7.3 行为变化记录

`PATH` 从写死改为继承后，原先因路径过短而无法定位的可执行文件（如 `/opt/homebrew/bin` 下的工具）将能正常启动。

可执行范围本身不变：`_ALLOWED_EXECUTABLES` 仍在第一层拦截。

`HOME` 被注入后，`git` 将开始读取 `~/.gitconfig`。若存在依赖「无全局 git 配置」的测试，需要显式隔离。

---

## 8. 测试要求

### 8.1 配置解析单测

新建 `tests/` 下对应模块，覆盖：

| 输入 | 期望 |
|---|---|
| 三项全空 | `inherit=core`，`include=()`，`set_values={}` |
| `INHERIT=CORE`（大写） | 解析为 `core` |
| `INHERIT=all` | 抛 `ValueError` |
| `INHERIT=bogus` | 抛 `ValueError` |
| `INCLUDE=A, B ,` | `("A", "B")` |
| `INCLUDE=A=B` | 抛 `ValueError` |
| `SET=K=V,K2=a=b` | `{"K": "V", "K2": "a=b"}` |
| `SET=K` | 抛 `ValueError` |
| 导出值与 `.env` 同时存在 | 取导出值，`sources` 记 `environment` |

### 8.2 `inherited_names` 单测

| inherit | include | 期望 |
|---|---|---|
| core | () | 等于 `CORE_ENV_NAMES` |
| core | ("KUBECONFIG",) | core + KUBECONFIG |
| core | ("PATH",) | 去重，无重复项 |
| none | () | `()` |
| none | ("KUBECONFIG",) | `("KUBECONFIG",)` |

### 8.3 `prepare_environment` 单测

用 monkeypatch 控制 `os.environ`：

| 场景 | 期望 |
|---|---|
| core 档，宿主有 `SSH_AUTH_SOCK` | 结果含该变量及其值 |
| core 档，宿主无 `SSH_AUTH_SOCK` | 结果不含该键，不抛异常 |
| none 档 | 结果仅含 `FALLBACK_ENV_VALUES` 与 include 命中项 |
| `set_values` 与继承值同名 | `set_values` 胜出 |
| 调用方 `environment` 与 `set_values` 同名 | 调用方胜出 |

### 8.4 sandbox 单测

| 场景 | 期望 |
|---|---|
| 策略授权 `GITHUB_TOKEN`，环境含该变量 | 通过 |
| 策略未授权，环境含 `GITHUB_TOKEN` | 拒绝 |
| 任意变量名含 `=` 或 NUL（含已授权） | 拒绝 |
| trust cache 写入抛 `OSError` | 决策仍返回，`diagnostics` 标记持久化失败，无异常穿透 |

### 8.5 端到端验收

在配置全空的前提下执行 `git push`（或只读等价命令 `git ls-remote --heads origin`），断言成功。

前提条件：本机 ssh-agent 已加载对应私钥。`executor.py` 使用 `stdin=DEVNULL`，任何需要交互输入 passphrase 或密码的凭据流程都会失败，不在本阶段处理范围。

### 8.6 回归

全量套件基线 `956 passed, 8 skipped`。重点检查断言 `prepare_environment` 返回值、`LocalProcessExecutor()` 无参构造、以及依赖固定 `PATH` 的进程测试。

---

## 9. 本阶段不做

- `inherit=all`（永不提供）
- 工作区级环境策略文件
- `AGT_PROCESS_ENV_EXCLUDE`
- 运行时能力申请、审批流程、discovery 动作
- 服务端适配（`AGT_RUNTIME_PROFILE`、trust cache 迁移、关闭 TOFU learning）见主 SPEC 第 10 节，已暂缓
- 交互式凭据输入（`stdin=DEVNULL` 限制）
- 完成判定改造（另见 `完成判定改造SPEC.md`）

---

## 10. 实施顺序

1. 第 3 节配置模块 + 8.1 / 8.2 单测（纯函数，可独立验证）
2. 第 4 节 POSIX executor + 8.3 单测
3. 第 5 节 Windows executor
4. 第 6 节 composition 装配
5. 第 7.1 节 sandbox 放行 + 8.4 单测
6. 第 7.2 节 trust cache 容错
7. 8.5 端到端验收
8. 8.6 全量回归

第 1 步完成后即可独立跑测试，不影响运行时行为。

---

## 11. 已知约束

**约束一：配置错误不会被发现。**

当前 `ToolResult.ok=true` 会掩盖 `ProcessResult.succeeded=false`，因此环境配置错误仍会以「Task 成功」收场。建议先实施 `完成判定改造SPEC.md`。

**约束二：与服务端部署的顺序限制。**

本阶段落地后，在完成主 SPEC 10.2 / 10.3 之前不得部署到服务端。原因：被投毒的 workspace trust cache 可让恶意 executable 通过准入，同时本阶段已将凭据变量注入其中。本地场景不受此限制。

**约束三：未验证项。**

7.2 的只读文件系统行为由代码推断得出，未在只读挂载实际验证。
