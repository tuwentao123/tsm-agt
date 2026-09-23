
## 2. 现状问题

### 2.1 executable trust 过度依赖路径

当前 Runtime 的 trust model 本质上是：

```text
workspace 内 executable => trusted
workspace 外 executable => denied
```

这种模型在传统项目中有效，但现代 Python toolchain 已经大量依赖 workspace 外解释器。

例如：

```text
.workspace/.venv/bin/python
 -> ~/.local/share/uv/python/.../python3.12
```

当 Runtime 执行 realpath resolve 后，会发现真实 executable 位于 workspace 外，因此直接拒绝执行。

### 2.2 executable trust 与 capability sandbox 混淆

当前 Runtime 已经存在多层安全机制：

- executable admission
- sandbox capability
- approval gate
- credential isolation（实际形态见下方核实说明）
- network restriction（实际形态见下方核实说明）

但用户最终只能看到统一的：

```text
PERMISSION_DENIED
```

这会导致：

- 用户误以为 git executable 不可信；
- 实际问题可能是 network egress 被禁止；
- 或 SSH credential 无法访问；
- 或 approval 未通过。

#### 核实说明（基于当前工作区代码）

上述两项的实际形态与「隔离层」的直觉不符，设计时必须以此为准：

**network restriction 当前不存在。**

`adapters/local_sandbox/sandbox.py` 文件头注释：

```text
deliberately not advertised as OS/container isolation: a
permitted compiler or interpreter still has the host permissions of tsm-agt
```

授权通过时的返回信息同样写明：

```text
approved structured command passed the local workspace command gate;
host filesystem and network are not OS-isolated
```

即当前 sandbox 是**命令准入门**，不是隔离层。子进程拥有 tsm-agt 进程的全部宿主权限，网络出口未受限制。本文档 4.4 与 7.2 节中把 network isolation 作为既有防线论述的部分，应理解为目标状态而非现状。

**credential isolation 的实际形态是「默认不传环境变量」。**

它由两个动作构成：

1. `local_process/executor.py:107` 只注入 `PATH` 与 `LANG`，宿主环境一概不继承；
2. `local_sandbox/sandbox.py:45` 的 `_CREDENTIAL_FRAGMENTS` 按变量名子串拒绝，仅覆盖 `secret` / `token` / `password` / `api_key` / `apikey`。

第 2 项并非有效防线：`SSH_AUTH_SOCK`、`KUBECONFIG`、`GITHUB_PAT` 均不匹配这些片段。真正起作用的是第 1 项。

因此第三阶段的本质不是「打穿隔离」，而是**把一个粗暴的清空动作，换成有授权记录的可选注入**。

**sandbox 边界只在第一层。**

`_ALLOWED_EXECUTABLES`（27 个名字）不含 `ssh`，但 `git push` fork 出的 `ssh` 不经过 `sandbox.authorize`。对 `git` 的授权隐含授权了它派生的任意子进程。

**阶段 1/2 的 TOFU 只覆盖 python。**

`_authorize_runtime_origin` 开头即 `if _PYTHON_NAME.fullmatch(executable_name) is None: return None`，其他 executable 仍只走 `_ALLOWED_EXECUTABLES` 静态枚举。

### 2.3 缺乏结构化诊断

Runtime 当前没有输出：

- requested executable
- resolved executable
- trust decision reason
- capability deny reason
- approval deny reason

导致调试成本很高。

## 3. 架构目标

本次演进目标：

1. 保持现有 Agent 主流程稳定；
2. 不弱化现有 sandbox 与 approval 安全边界；
3. 提升现代 Python toolchain 兼容性；
4. 让 Runtime 的 trust decision 可解释；
5. 将 executable trust 与 capability control 解耦；
6. 为后续 production-grade Runtime trust 打基础。

非目标：

- 不移除 realpath resolve；
- 不默认允许所有外部 executable；
- 不取消 git push 的 network sandbox；
- 不引入无限扩张 allowlist；
- 不重写核心 execution pipeline。

## 4. 方案设计

### 4.1 Runtime 安全层分层结构

建议将 Runtime 安全层明确拆分为：

```text
Executable Trust Layer
    ↓
Interpreter Origin Trust
    ↓
Capability Sandbox
    ↓
Approval Layer
    ↓
Diagnostics Layer
```

各层职责：

| 层级 | 职责 |
|---|---|
| Executable Trust | 是否允许启动 executable |
| Interpreter Origin Trust | executable 来源是否合法 |
| Capability Sandbox | 运行后允许访问哪些能力 |
| Approval Layer | 外部副作用是否需要审批 |
| Diagnostics Layer | 解释 Runtime 为什么拒绝 |

### 4.2 保留 realpath 与 symlink follow

必须继续保留：

```text
realpath resolve
symlink follow
```

否则会出现：

```text
workspace/.venv/bin/python
 -> /tmp/malicious
```

绕过 trust boundary 的问题。

因此：

- workspace 内路径不等于 trusted；
- trust 必须针对 resolved executable；
- Runtime 必须检测真实 origin。

### 4.3 trusted runtime origins（短期方案）

短期引入：

```text
trusted runtime origins
```

例如：

```text
~/.local/share/uv/python/
~/.pyenv/
/opt/homebrew/
/usr/bin/python3
```

执行逻辑：

```text
workspace executable trusted
OR
resolved executable 属于 trusted origins
ELSE deny
```

同时必须结合：

- realpath normalize
- owner uid 校验
- executable permission 校验
- world-writable protection

避免简单字符串 contains 判断。

### 4.4 capability sandbox 保持独立

即使 executable trusted：

```text
uv python => trusted
```

也不代表：

```text
network => allowed
credential => allowed
git push => allowed
```

因此 capability sandbox 必须继续独立存在。

Runtime 需要明确区分：

```text
Executable rejected
```

与：

```text
Capability denied
```

两类错误。

### 4.5 Diagnostics Layer

Runtime 应增强错误输出。

示例：

```text
Executable rejected

requested:
.workspace/.venv/bin/python

resolved:
~/.local/share/uv/python/cpython-3.12/bin/python3.12

reason:
resolved executable origin is outside trusted runtime set
```

另一类：

```text
git push denied

reason:
network egress capability not granted
```

避免所有错误统一折叠成 `PERMISSION_DENIED`。

## 5. trusted runtime origins 设计评估

### 5.1 为什么短期可以采用枚举白名单

在当前阶段：

- runtime 来源有限；
- 场景仍可控；
- 目标是修复 uv/pyenv/Homebrew 兼容性；
- 不希望改动核心 execution flow；

因此：

```text
trusted runtime origins = allowlist
```

是一种风险较低、易落地的方案。

其优势：

- 行为可预测；
- trust boundary 清晰；
- 可审计；
- 与现有 trust model 连续演进；
- 不影响 capability sandbox。

### 5.2 纯枚举方案的长期问题

生产环境中 runtime 来源不可预测。

未来可能出现：

- conda
- nix
- rye
- asdf
- mise
- 企业内部 runtime
- remote interpreter
- CI ephemeral runtime
- container runtime

如果持续扩大 allowlist，会逐渐导致：

```text
trust boundary widening
```

最终可能演化成过宽路径授权。

因此：

纯枚举不适合作为长期最终方案。

### 5.3 推荐演进路径

推荐采用渐进式升级：

```text
路径 allowlist
    ↓
TOFU 动态信任
    ↓
fingerprint/signature trust
    ↓
capability-scoped trust
    ↓
provenance + risk scoring
```

最终目标不是：

```text
这个 executable 能不能运行
```

而是：

```text
这个 executable 被允许接触哪些能力
```

## 6. TOFU 动态信任方案

生产阶段推荐引入：

```text
TOFU（Trust On First Use）
```

当 Runtime 遇到未知 executable：

1. resolve realpath；
2. 收集 metadata；
3. 计算 fingerprint；
4. 请求用户 approval；
5. 按 workspace/project/user scope 缓存 trust。

示例：

```text
Requested executable:
.workspace/.venv/bin/python

Resolved to:
~/corp/python/3.12/bin/python3.12

Unknown runtime origin.

Approve for:
- this execution
- this workspace
- this project
- current user
```

这样 Runtime 不需要预知所有 runtime 来源。

## 7. 风险与兼容性分析

### 7.1 风险

主要风险包括：

- allowlist 无限扩张；
- 错误信任 world-writable executable；
- executable trust 与 capability sandbox 耦合；
- 为兼容 uv 而误放宽 network capability。

### 7.2 风险控制

必须保留：

- realpath resolve；
- symlink follow；
- capability sandbox；
- approval gate；
- network isolation；
- credential isolation。

trusted runtime origins 只解决：

```text
是否允许启动 executable
```

不负责：

```text
启动后允许访问哪些能力
```

### 7.3 对核心流程影响

本次改造主要位于 Runtime 安全层。

理论上不会影响：

- agent loop
- task scheduler
- tool routing
- execution pipeline
- checkpoint flow

核心变化集中在：

```text
Runtime executable admission decision
```

因此属于低侵入式安全层演进。

## 8. 后续演进建议

### 第一阶段（当前工作区已实施）

状态：✅ 已完成基础落地（已基于当前工作区重新验证）

当前已实施内容：

- 引入 trusted runtime origins；
- 修复 uv/pyenv/Homebrew compatibility；
- 增强 diagnostics；
- 区分 executable denied 与 capability denied；
- 增加 runtime origin realpath 校验；
- 增加 owner/executable/world-writable 安全检查；
- 增加 sandbox diagnostics 输出与测试覆盖。

当前工作区已验证存在的实现包括：

- `LocalWorkspaceSandbox._authorize_runtime_origin`
- trusted runtime origin allowlist
- `SandboxDecision.diagnostics`
- runtime origin diagnostics 输出
- sandbox tests 验证

说明：

当前实现本质上仍属于：

```text
静态 trusted runtime origins
+ runtime origin 校验
+ diagnostics 增强
```

属于 Runtime trust 架构演进的“第一阶段基础版本”，并非完整 production-grade trust architecture。

### 第二阶段（当前已实施基础版本）

状态：✅ 已完成基础版 TOFU trust 能力

当前已实施内容：

- 引入 TOFU 动态 trust；
- 增加 workspace-scoped runtime trust cache；
- 引入 executable fingerprint；
- 增加 fingerprint drift 检测；
- 增加 persisted TOFU diagnostics。

当前实现说明：

- 首次出现的非 trusted-origin runtime 会基于 TOFU 自动登记；
- trust state 持久化到 workspace `.tsm/runtime_trust.json`；
- 后续执行会校验 executable fingerprint；
- fingerprint 变化会触发 runtime deny；
- 当前仍未实现 signer/provenance trust。

可行性说明：

仅实施第一阶段：

```text
可行。
```

适用于：

- 本地开发环境；
- uv/pyenv/Homebrew 兼容修复；
- 当前 Runtime 的低侵入式安全增强；
- 保持现有 capability sandbox 架构稳定。

但限制在于：

- runtime 来源仍需要静态 allowlist；
- 无法自动学习未知 runtime；
- 没有 trust persistence；
- 没有 fingerprint/signature trust；
- 不适合复杂 production runtime 环境。

仅实施第一阶段 + 第二阶段：

```text
同样可行，并且已经能够形成较完整的 Runtime trust 基础架构。
```

阶段1+2 能解决的核心问题：

- 动态 runtime trust；
- workspace/project-scoped trust 生命周期；
- 大量减少 allowlist 扩张；
- 提升未知 runtime 的可用性；
- 为后续 provenance/risk engine 打基础。

但阶段1+2 仍然不等于完整 production-grade Runtime security system，因为仍缺少：

- capability-scoped trust；
- signer/provenance verification；
- risk scoring；
- adaptive sandbox；
- enterprise trust federation。

### 第三阶段：进程环境继承策略（process environment policy）

状态：⬜ 未实施

> 本节已按「服务端 + 本地双场景」重新设计。早期草案中的运行时能力审批、discovery 动作、`.tsm/env_policy.json` 与 capability profile 分组均已废弃，原因见 3.8 对标说明。

#### 3.1 要解决的问题

阶段 1/2 解决的是「这个 executable 能不能启动」。阶段 3 解决的是「启动后能拿到宿主的什么东西」。

当前实现（`adapters/local_process/executor.py:107`）：

```python
def prepare_environment(self, environment):
    safe = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
    }
    safe.update(environment)   # environment 来自模型显式传参
    return safe
```

子进程能拿到的宿主资源是硬编码的两个变量。宿主的 ssh-agent、git credential helper、kubeconfig、docker socket 一概不可见。

直接后果：`git push` 因为拿不到 `SSH_AUTH_SOCK` 而报 `Permission denied (publickey)`，而同一台机器上用户在终端里可以正常 push。

#### 3.2 「宿主」的准确含义

`os.environ` 取到的是 **tsm-agt Agent 进程自身继承到的环境**，不是整台机器的全局状态。它完全取决于 Agent 的启动方式。

当前工作区实测（web 服务进程）：

```text
SSH_AUTH_SOCK=/var/run/com.apple.launchd.<id>/Listeners
GIT_ASKPASS=/Applications/Kiro.app/.../askpass.sh
HOME=/Users/<user>
```

因为服务由终端启动，继承了 launchd 的 ssh-agent socket，所以 `SSH_AUTH_SOCK` 可用。

推论：如果改由 launchd / systemd / 容器启动 Agent 而未显式传入该 socket，本阶段会在「宿主不存在该变量」处失败。那种情况的解法是调整 Agent 的启动方式，不是修改本阶段设计。

#### 3.3 设计原则

1. **静态配置驱动，模型不参与**。环境策略在启动时确定，不提供运行时申请、审批或 discovery 动作。
2. **不枚举业务能力**。不定义 `ssh_agent` / `cloud_identity` 这类类别，避免重现 `_ALLOWED_EXECUTABLES` 与 `_CREDENTIAL_FRAGMENTS` 的枚举扩张。
3. **配置的原子单位是环境变量名**。Runtime 不理解变量语义，只做名字匹配与存在性检查。
4. **策略来源必须是部署方，不能是工作区内容**。工作区可能来自 clone 或用户上传，属于不可信输入。
5. **默认开箱可用**。以 `core` 为默认档，用户零配置即可执行常规开发命令。收紧由 `none` 提供。
6. **区分「基线」与「凭据本体」**。判断依据是值本身是否为秘密，而非变量名像不像凭据。
7. **值永不回显**。变量值只进 `subprocess.env`，不进模型上下文、事件 payload、日志。

##### 关于原则 5 的修正依据

早期草案以 `none` 为默认、要求用户显式配置 `SSH_AUTH_SOCK`。该结论在实测后被推翻。

**实测一：注入单个变量即可通过 GitHub SSH 认证**

以 `env -i` 模拟 Agent 环境，对远端执行只读认证：

```text
A. 仅 PATH + LANG（当前实现行为）
   git@github.com: Permission denied (publickey).

B. 加 SSH_AUTH_SOCK
   d46ebdd  refs/heads/codex/multiturn-chat-core-refactor
   abe6ffa  refs/heads/main

C. 加 SSH_AUTH_SOCK + HOME
   同 B
```

结论：只需 `SSH_AUTH_SOCK`。`HOME` 非必需，OpenSSH 在 `HOME` 未设时回退 `getpwuid()` 定位 `~/.ssh`。

**实测二：私钥文件本身在当前环境下已可读**

在仅有 `PATH` + `LANG` 的环境下启动子进程，尝试读取私钥：

```text
home via getpwuid: /Users/<user>
readable: id_ed25519_github         | encrypted-marker: False
readable: id_ed25519_github_tsm_agt | encrypted-marker: False
readable: id_rsa                    | encrypted-marker: False
```

子进程以相同 uid 运行，未加密私钥文件一直可读。这与本文档 2.2 核实说明中引用的 sandbox 自述一致：被准入的解释器拥有 tsm-agt 的宿主权限。

但**不可据此认为扣留 `SSH_AUTH_SOCK` 的安全收益为零**。可读文件不等于可直接使用：使用私钥仍需自行实现签名或将其交给 ssh 客户端，并非顺手可得。因此扣留 socket 仍有一定收益，只是弱于直觉预期。

将 `SSH_AUTH_SOCK` 纳入基线档的真正依据是：真正的边界是 executable allowlist 与工作区路径限制；要建立实际凭据边界需要独立 uid 或容器隔离，属另一量级工作。在当前「命令准入门而非隔离层」的架构下，该变量的边界价值不足以抵消它造成的可用性缺陷。

**实测三：本机场景对 ssh-agent 是强依赖**

```text
ssh-add -l
  256 SHA256:g/g1mnc0... <user>@users.noreply.github.com (ED25519)

仅注入 SSH_AUTH_SOCK
  Hi <user>! You've successfully authenticated, but GitHub does not provide shell access.

不注入 SSH_AUTH_SOCK（仅依赖磁盘上的私钥）
  git@github.com: Permission denied (publickey).
```

第三组说明：即便磁盘上存在多个未加密私钥，缺少 agent 仍无法认证。原因是私钥文件名非 ssh 默认名（`id_ed25519_github` 而非 `id_ed25519`），其映射关系写在 `~/.ssh/config` 中，而该配置在此环境下未生效。

结论：`SSH_AUTH_SOCK` 对本机 Git 场景是必要条件，不是可选优化。

#### 3.4 配置形态

复用现有 `src/tsm_agt/bootstrap/*_configuration.py` 范式（参照 `egress_configuration.py`、`context_configuration.py`），不引入任何新的文件格式：

```text
AGT_RUNTIME_PROFILE=local|server
AGT_PROCESS_ENV_INHERIT=core|none        # 默认 core
AGT_PROCESS_ENV_INCLUDE=KUBECONFIG,...   # core 之外的补充，默认空
AGT_PROCESS_ENV_SET=KEY=VALUE,...        # 显式覆盖，默认空
```

装配路径：

```text
.env 或进程环境（导出值覆盖文件值）
    ↓ bootstrap 读取并校验
ProcessEnvironmentConfiguration（frozen dataclass）
    ↓ composition.py 注册进 registry
Kernel 依赖
    ↓ 调用时传入
executor.prepare_environment(validated_environment, policy)
```

**零配置即可用**：三个变量全部留空时取默认值 `core`，常规开发命令（含 `git push`）可直接执行。

命名说明：现有变量均为 `TSM_AGT_*` 前缀，本阶段新增变量按决议采用 `AGT_*`，两种前缀会并存。若后续统一，应整体迁移而非逐个例外。

##### 三档定义

| 档位 | 行为 | 用途 |
|---|---|---|
| `core` | 传递基线变量，宿主存在则取、不存在则跳过 | **默认**。开箱可用 |
| `none` | 只给 `PATH` 与 `LANG` 固定值，等于现状 | 需要严格控制的场景 |
| `all` | **不提供** | 会把宿主的数据库密码、云凭据一并交给子进程；Agent 派生子进程数量大，风险不可控 |

##### core 清单

代码内常量，非配置项，用户无需声明：

```text
PATH  HOME  LANG  LC_ALL  TZ  TMPDIR  SSH_AUTH_SOCK
```

入选判据是**值本身是否为凭据**：

| 类型 | 举例 | 是否入 core |
|---|---|---|
| 路径、locale、标识 | `HOME`、`TMPDIR`、`SSH_AUTH_SOCK` | 是 |
| 凭据本体 | `GITHUB_TOKEN`、`AWS_SECRET_ACCESS_KEY` | 否，需显式 `include` |

`SSH_AUTH_SOCK` 的值是 socket 路径而非密钥，且使用它需要相同 uid——子进程本已具备。依据见 3.3 实测二与实测三。

已明确排除：`TERM`（会使部分工具输出 ANSI 控制序列污染采集到的日志）、`SHELL`。

##### 为何 core 是清单而非规则

理想形态是用规则表达「继承所有值不是凭据的变量」，但「值是否为凭据」无法自动判定——按变量名猜测即回到 `_CREDENTIAL_FRAGMENTS` 的老路。因此采用「能确定的写死，不能确定的交给用户」：

```text
core     代码常量，7 项，覆盖绝大多数场景
include  用户配置，覆盖其余场景，零代码改动
```

某变量应入 core 还是 include，判据为「是否所有项目都需要」。例如 `KUBECONFIG` 仅 k8s 项目需要，归 include。

##### core 与 `_ALLOWED_EXECUTABLES` 的区别

core 同样是枚举，但两者的增长性质完全不同：

| | `_ALLOWED_EXECUTABLES` | core 清单 |
|---|---|---|
| 枚举对象 | 业务工具（git / node / cargo / flutter…） | POSIX 进程基线 |
| 增长动力 | 每支持一个新工具即需新增 | 无。该集合长期稳定 |
| 当前状态 | 27 项，且已不足（含 `ruff` 但不含 `ssh`、`uv`） | 7 项 |

`PATH`、`HOME`、`LANG`、`TZ`、`TMPDIR` 属 POSIX 标准范畴，不因接入新工具而需扩充。`SSH_AUTH_SOCK` 是唯一工具相关项，其正当性来自 ssh-agent 协议已是事实标准。

##### `PATH` 的行为变化

现状为写死 `/usr/bin:/bin:/usr/sbin:/sbin`，不含 `/opt/homebrew/bin`，因此 homebrew 安装的工具在子进程中无法定位。改为继承后该隐性缺陷一并修复。

需注意这会改变既有行为：原先因 `PATH` 过短而「找不到」的可执行文件将能正常启动。可执行范围本身不变，`_ALLOWED_EXECUTABLES` 仍在第一层拦截。

若不希望改变该行为，可将 `PATH` 排除在 core 之外并保留写死值，其余六项照 core 处理。本 SPEC 采纳继承方案。

##### `include` 的定位

`include` 仅用于补充 core 之外的变量，例如项目所需的 `KUBECONFIG` 或企业自定义变量。多数使用者无需配置该项。

#### 3.5 双场景策略

一套机制，两组默认值。差异只有「默认值」和「是否信任工作区内的文件」两点。

| 维度 | local | server |
|---|---|---|
| `inherit` 默认 | `core` | `core`（可按部署收紧为 `none`） |
| `include` 默认 | 空 | 空 |
| 读工作区级配置 | 允许（第二批） | 不读 |
| 读工作区 trust cache | 允许 | 不读 |
| trust cache 写入位置 | 工作区 `.tsm/` | 部署方目录，按工作区路径分区 |
| 未知 runtime 首次出现 | TOFU 登记放行 | 不学习，只认部署配置中的 origins |
| 只读文件系统 | 不预期 | 必须容错 |

核心原则：**server 模式下工作区内容一律视为不可信输入。**

该开关同时允许在本地复现服务端行为，便于测试。

#### 3.6 改造注入点

现状调用顺序（`core/kernel.py:3641`）：

```python
resolved_cwd = self._resolve_process_cwd(Path(task.workspace), cwd)
validated_environment = self._validate_process_environment(environment or {})
safe_environment = dict(executor.prepare_environment(validated_environment))
sandbox_request = SandboxRequest(argv, resolved_cwd, safe_environment)
decision = await self._dependencies.sandbox.authorize(sandbox_request)
```

`prepare_environment` 先执行、sandbox 后审查，该顺序正确，不需调整。改造后：

```python
def prepare_environment(self, environment, policy) -> dict[str, str]:
    safe = dict(policy.fallback_values)      # inherit=none 时的固定 PATH / LANG
    for name in policy.inherited_names:      # core 清单 + include 补充
        value = os.environ.get(name)
        if value is not None:
            safe[name] = value
    safe.update(policy.set_values)
    safe.update(environment)                 # 模型显式传参优先级最高
    return safe
```

`policy.inherited_names` 在 `inherit=core` 时为 core 清单与 `include` 的并集，在 `inherit=none` 时仅为 `include`。宿主不存在的名字静默跳过，不报错。

签名变更波及 `ProcessExecutorPort` 契约与 `local_process`、`windows_process` 两个实现及相关测试。已决议直接改签名，不做可选参数兼容。

#### 3.7 `_CREDENTIAL_FRAGMENTS` 必须同步调整

`adapters/local_sandbox/sandbox.py:45`：

```python
_CREDENTIAL_FRAGMENTS = ("secret", "token", "password", "api_key", "apikey")
```

两个问题：

1. **它会拦掉已授权的变量。** 因为 `prepare_environment` 先跑，注入的变量会进入 `SandboxRequest.environment`。一旦配置了 `GITHUB_TOKEN`，会被自己的 sandbox 拒绝。
2. **它本来就不是有效防线。** `SSH_AUTH_SOCK`、`KUBECONFIG`、`GITHUB_PAT` 均不匹配这些片段。同类经验可参考 Hermes 的 [issue #7071](https://github.com/NousResearch/hermes-agent/issues/7071)：其输出脱敏可用 base64 / hex / 反转字符串绕过，说明字符串匹配不足以作为凭据边界。

调整方式：由策略显式放行的名字（core 清单与 `include` 的并集）视为已授权，跳过该检查；其余仍然拦截。黑名单退化为「兜底防误传」，不再阻碍显式配置。

若在诊断信息中输出被拦名字，可以写变量名，**不得写变量值**。

#### 3.8 对标说明（为何废弃运行时审批与 discovery）

内容为符合许可要求已改写。

**Codex** 使用 `shell_environment_policy` 在配置文件中声明：`inherit`（`all`/`core`/`none`）、`exclude`（glob，含针对密钥类名字的默认排除）、`set`、`ignore_default_excludes`。来源：[Advanced Configuration](https://developers.openai.com/codex/config-advanced/)、[issue #7521](https://github.com/openai/codex/issues/7521)。

三点值得注意：

1. 默认是继承后再排除，与本项目「默认只给 PATH + LANG」相反；
2. 没有运行时审批，也没有 discovery，模型不参与环境策略决策；
3. `exclude` 是黑名单，社区正在请求 allowlist 能力（[issue #22023](https://github.com/openai/codex/issues/22023)），理由是自定义变量名差异大，需要 prefix/glob/regex 支持。

另有 [issue #18248](https://github.com/openai/codex/issues/18248) 记录 Windows 上核心变量缺失导致 dotnet/NuGet 与 git 网络功能失败——与本项目 `git push` 失败同属「环境给得过少」。

**Hermes** 从进程环境与 `~/.hermes/.env` 读取凭据，并通过 Secret Source 插件在启动时从外部密钥管理器解析到环境变量；其隔离手段是容器，不是环境变量筛选。来源：[环境变量参考](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)、[Secret Source Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/secret-source-plugin)、[security 文档](https://hermes-agent.nousresearch.com/docs/user-guide/security)。

结论：两者都采用**静态配置 + 启动时确定**。本项目直接做 allowlist 反而比 Codex 的「默认继承 + 黑名单排除」起点更安全，无需经历由宽到严的过程。因此运行时审批、discovery、capability profile 分组全部废弃。

**保留的唯一模型相关能力**是诊断输出：命令失败且 `inherit=none`（或所需变量不在策略放行集合内）时，提示可能缺少环境继承，并给出可直接复制的配置行。这属于第 4.5 节 Diagnostics Layer，不是审批流程。

采用 `core` 默认档后该提示的重要性下降——常规场景已无需配置——但对显式收紧为 `none` 或需要 core 之外变量的场景仍然必要。

#### 3.9 值不回显的核实结果

已核对三处，当前不存在回显路径：

| 落点 | 结论 |
|---|---|
| 事件 payload | `process.started` / `process.denied` / `process.failed` 均不含 environment，`argv` 亦只存 `argv_hash` |
| `ProcessStartRequest` | `environment` 仅传给 executor 用于 `create_subprocess_exec(env=...)`，不进事件 |
| `ToolResult.data` | `process_tools.py` 仅返回 `mode` / `succeeded` / `failure_code` / `status` / `exit_code` |

本阶段实施时须保持该性质，并在新增诊断输出中确认只含变量名。

#### 3.10 与完成判定改造的关系

早期草案让模型在失败后申请能力，因此强依赖《完成判定改造SPEC》。改为静态配置后，本阶段**不再强依赖**它即可使 `git push` 成功。

但两者仍应先做完成判定改造，原因：当前 `ToolResult.ok=true` 会掩盖 `ProcessResult.succeeded=false`，导致任何环境或凭据问题都以「Task 成功」收场，配置错误无法被发现。

#### 3.11 顺带修复：trust cache 写入容错

本项与环境策略无直接关系，但属于同一改造范围内的低成本修复，且**与部署形态无关**——本地同样会触发。

`_store_runtime_trust_cache`（`adapters/local_sandbox/sandbox.py:319`）直接写入，未捕获异常：

```python
cache_path.parent.mkdir(parents=True, exist_ok=True)
cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
```

本地触发条件：工作区位于只读挂载、磁盘写满、`.tsm/` 权限不正确。此时 `OSError` 会穿透 `authorize`，表现为一次无法解释的执行失败，而非结构化的 sandbox 决策。

改动：捕获写入异常，降级为「本次决策生效但不持久化」，并在 `SandboxDecision.diagnostics` 中记录持久化失败原因。不得让异常穿透授权路径。

注：该问题由代码推断得出，未在只读文件系统实际验证。

#### 3.12 本阶段不包含

移至第四阶段：

- provenance-aware runtime
- signer verification
- runtime risk scoring
- adaptive sandbox

第二批可选项（第一批不做）：

- 工作区级环境策略（仅允许收窄，`include` 取交集而非并集；server 模式忽略）
- `AGT_PROCESS_ENV_EXCLUDE`（从 core 中剔除个别变量）

永不提供：`inherit=all`。

### 第四阶段

- production-grade Runtime security policy engine；
- 动态风险分析；
- adaptive sandbox；
- enterprise trust federation。

## 9. 总结

当前 Runtime 的核心问题不是“安全限制过多”，而是：

- trust boundary 仍停留在路径时代；
- executable trust 与 capability sandbox 缺乏结构化表达；
- diagnostics 不足；
- 现代 Python toolchain 已经超出原有 trust model 假设。

因此本次演进的核心方向是：

```text
从路径信任
    ↓
演进到来源 + 能力 + 风险联合决策。
```

短期采用有限 trusted runtime origins 是合理的工程方案；长期则需要逐步升级为 production-grade provenance-aware runtime trust architecture。

## 10. 服务端部署适配（阶段 1/2 的补充改动）

状态：⏸ **暂缓。当前优先本地场景，本节留待部署服务端前再实施。**

本节为后补章节。原文档默认本地开发场景，阶段 1/2 的实施在该前提下成立；一旦部署到服务端，以下几处必须调整。

### 10.0 暂缓决议与前提

暂缓的依据是改动局部、不触及架构：

| 改动 | 预估范围 |
|---|---|
| A：trust cache 读写位置可配置 | 1 处路径构造 |
| B：server 模式跳过 TOFU learning | 1 个分支 |
| 开关：`AGT_RUNTIME_PROFILE` | 1 个 bootstrap 配置模块 |

维持该范围有一个**硬前提**：

> 策略与信任的读取位置不得写死在工作区路径上。

具体要求：

- 环境策略只能经 bootstrap 注入（见 3.4 / 3.6），`prepare_environment` 接收已构造好的 policy 对象，不得在 executor 内直接读取工作区文件；
- 第一批不引入工作区级环境策略文件（见 3.12 第二批清单）。

若违反该前提，服务端适配将从「换一处读取来源」变成「改 executor + 改 Port + 改测试」。

原 10.4（只读文件系统容错）已前移至阶段 3 实施，理由见 3.11：该问题与部署形态无关，本地同样会触发。

### 10.1 部署形态开关

```text
AGT_RUNTIME_PROFILE=local|server
```

`local` 保持现有行为。`server` 下的核心约束：**工作区内容一律视为不可信输入**，因为它可能来自 git clone 或用户上传。

### 10.2 改动 A：trust cache 的位置与信任来源

**问题**

`_load_runtime_trust_cache`（`adapters/local_sandbox/sandbox.py:288`）从工作区读取：

```python
cache_path = workspace_root / ".tsm/runtime_trust.json"
```

判定时只比对指纹，**不校验该记录由谁写入**：

```python
cache = self._load_runtime_trust_cache(workspace_root)
runtime_record = cache["runtimes"].get(runtime_key)
cached_fingerprint = runtime_record.get("fingerprint") ...
if cached_fingerprint == fingerprint:
    return SandboxDecision(True, "runtime trusted via persisted TOFU fingerprint")
```

因此任何能向工作区写文件的一方都可以预置一条 trust 记录：

```json
{
  "version": 2,
  "runtimes": {
    "/tmp/evil/python3": {"fingerprint": "<该文件的真实 sha256>", "trust_source": "tofu"}
  }
}
```

下次执行会直接命中 `persisted-tofu` 分支，TOFU 的首次校验被完全跳过。

本地场景下风险有限（能写入工作区的一方已具备其他手段）；服务端场景下这是可投毒的信任源。

**改动**

trust cache 路径由部署形态决定。`server` 模式读取部署方控制的目录（按工作区路径分区），不读取工作区内任何 trust 文件。

### 10.3 改动 B：未知 runtime 的首次行为

**问题**

当前实现为静默登记并放行：

```python
if cached_fingerprint is None:
    cache["runtimes"][runtime_key] = {...}
    self._store_runtime_trust_cache(workspace_root, cache)
    return SandboxDecision(True, "runtime trusted via TOFU workspace cache")
```

第 6 节描述的是「请求用户 approval」，实际实现没有审批环节，等于 Trust Without Asking。

服务端没有可交互的人，「首次使用即信任」等同于无条件信任。

**改动**

`server` 模式不进行 runtime trust learning，只认部署配置中声明的 origins 与指纹。未知 runtime 直接拒绝，并输出结构化诊断说明需在部署配置中登记。

`local` 模式保持现有 TOFU 行为。

**对标说明**（内容为符合许可要求已改写）

成熟实现在服务端不做 Agent 级 executable trust learning：

- Codex 提供 `external-sandbox` 模式，自身不强制沙箱，假定调用方已完成隔离，仅继续跟踪网络访问。来源：[codex-sandboxing 说明](https://gist.github.com/rtzll/8ec03ad8a4cca3ae43ce3db7eb7dcc09)
- Hermes 的隔离手段是容器，而非 executable 筛选。来源：[security 文档](https://hermes-agent.nousresearch.com/docs/user-guide/security)

服务端「部署方预先授权」的实际操作形式有三种，均为构建期确定而非运行期学习：

1. 镜像内固定工具链。Dockerfile 装好 python / git / node，路径确定，origins 配置指向镜像内路径；镜像未安装的程序不可能被执行。
2. 配置下发显式清单。允许的 origins 或指纹随部署注入（configmap / 环境变量），运行时只读。
3. 容器隔离兜底。即使前两层被绕过，影响范围限于容器内。

推论：阶段 1/2 的 TOFU 本质是本地开发构造。服务端更合理的形态是「容器隔离 + 镜像内固定工具链 + 关闭 trust learning」，而不是把 TOFU 改造成适应服务端。

### 10.4 改动 C：已前移至阶段 3

原「只读文件系统容错」不再属于服务端专有改动，已前移至 3.11 随阶段 3 一并实施。

### 10.5 阶段 1/2 的其他已知局限

以下为核实结果，不属于服务端专有问题，但影响阶段 3/4 的设计前提：

**沙箱边界只在第一层进程。**
`_ALLOWED_EXECUTABLES`（27 个名字）不含 `ssh`，但 `git push` fork 出的 `ssh` 不经过 `sandbox.authorize`。对 `git` 的授权隐含授权了它派生的任意子进程。

**TOFU 只覆盖 python。**
`_authorize_runtime_origin` 开头即 `if _PYTHON_NAME.fullmatch(executable_name) is None: return None`。其余 executable 仍只走 `_ALLOWED_EXECUTABLES` 静态枚举，`_TRUSTED_RUNTIME_ORIGINS`（4 个路径）与 fingerprint 机制对它们不生效。

**origins 为硬编码。**
建议允许由部署配置提供，而非仅硬编码 4 个路径。

### 10.6 实施顺序

当前决议的顺序（本地优先）：

```text
《完成判定改造SPEC》
    ↓ 提供 effect_status = FAILED，使失败不再被判定为成功
阶段 3（进程环境继承策略，含 3.11 写入容错）
    ↓ 使合法动作获得所需凭据，本地场景闭环
────────── 以下留待部署服务端前 ──────────
阶段 1/2 服务端适配（10.2 / 10.3 + AGT_RUNTIME_PROFILE）
    ↓ 保证 server 模式下 trust 不可投毒
阶段 4
```

**风险记录**：在服务端场景下，先有阶段 3 而未做 10.2 会放大风险——被投毒的 trust cache 让恶意 executable 通过准入，同时环境策略又将凭据变量注入给它。两个单独可控的问题叠加后后果严重。

因此本决议成立的边界是：**阶段 3 落地后，在完成 10.2 / 10.3 之前不得部署到服务端。** 本地场景不受此限制，因为工作区本身即用户自有目录。
