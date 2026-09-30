# MCP Server 开发提示词 — Pet Hospital 数据服务

> 协议版本：**MCP 2026-07-28** | 语言：**Python** | MVP 范围：**仅 `list_pets`**

---

## 一、协议版本与核心约束（必须严格遵循）

**目标协议版本：`2026-07-28`**

### 关键协议约束

1. **无状态协议**：移除 `initialize`/`notifications/initialized` 握手和 `Mcp-Session-Id` 头。每个请求必须在 `_meta` 中携带 `io.modelcontextprotocol/protocolVersion` 和 `io.modelcontextprotocol/clientCapabilities`。服务器必须在每个响应的 `_meta` 中返回 `io.modelcontextprotocol/serverInfo`。
2. **`server/discover` 是 MUST 实现**：客户端可预先调用此 RPC 获取服务器支持的协议版本、能力和身份信息。Python SDK v2 自动注册。
3. **请求头要求**：Streamable HTTP 请求必须包含 `Mcp-Method` 和 `Mcp-Name` 头，且与 body 一致。服务器必须拒绝头与 body 不匹配的请求（返回 `-32020` `HeaderMismatch`）。
4. **列表结果缓存**：`tools/list` 必须返回 `ttlMs` 和 `cacheScope`（`CacheableResult` 接口）。工具列表应按名称字典序排列，确保确定性顺序。
5. **MRTR（多轮请求）**：如果工具执行中需要额外信息，返回 `resultType: "input_required"` 和 `inputRequests`，客户端重试时带上 `inputResponses`。
6. **已移除的方法**：`ping`、`logging/setLevel`、`notifications/roots/list_changed`。`subscriptions/listen` 替代旧的订阅端点。
7. **已废弃功能**（禁止使用）：Roots、Sampling、Logging、HTTP+SSE 传输、Dynamic Client Registration。
8. **`resultType` 字段**：每个结果必须包含 `resultType: "complete"` 或 `"input_required"`。
9. **错误码分配**：`-32020` 到 `-32099` 为规范保留，`-32000` 到 `-32019` 为实现定义。

---

## 二、项目背景与数据模型

**项目**：宠物医院管理系统（Pet Hospital REST API），Go 标准库编写，单文件数据库 `data/pet.db`。

**数据库格式**：
```
┌ Header 32B ─────────────────────────────────┐
│ magic "PETDBv1\n" | version u32 | flags u32 │
├ Record 记录区（顺序追加）───────────────────┤
│ [len u32][crc32 u32][json payload]          │
│ payload: {"op":"put|del","id":"...","data":{}} │
└─────────────────────────────────────────────┘
```

- 读：启动时回放日志，重建内存 map 索引
- 写：追加记录 + `fsync`
- 损坏自愈：CRC 校验失败或长度异常时截断到最后一个完整记录

**内置数据**：`data/pet.db` 含 1008 条模拟数据（8 条手工精选 + 1000 条随机）。

**Pet 数据模型：**
```
Pet {
  id: string          // "PET-000001"，自动生成
  name: string        // 宠物姓名
  species: string     // 种类（犬、猫、兔等 12 种）
  breed: string       // 品种
  gender: string      // 性别（公/母）
  ageMonths: int      // 月龄
  color: string       // 毛色
  chipId: string      // 芯片号（20% 无）
  ownerName: string   // 主人姓名
  ownerPhone: string  // 电话
  address: string     // 住址
  doctor: string      // 主治医生（12 位）
  disease: string     // 疾病（46 种）
  status: string      // 就诊状态（待就诊/就诊中/住院中/已康复/慢性病随访）
  allergy: string     // 过敏史（多数为「无」）
  records[]: Record[] // 历史病历
  charges[]: Charge[] // 消费明细
  totalCost: float    // 派生字段 = charges[].amount 之和
  visitCount: int     // 派生字段 = records.length
}

Record {
  doctor: string
  diagnosis: string
  symptoms: string
  treatment: string
  prescription: string[]
  charge: float
  date: string
}

Charge {
  item: string
  category: string    // 检查/药品/手术/住院/疫苗/护理/其他
  amount: float
  doctor: string
  date: string
}
```

**模拟数据生成规则**：
- 疾病与物种匹配（不会出现「仓鼠股骨骨折」）
- 医生分配按专长
- 费用分层合理（重症高、复查递减）
- 重症患者有 2-5 次随访
- 20% 宠物未植入芯片，5% 有备注

---

## 三、MVP 范围：仅实现 `list_pets` 一个 Tool

### Tool 定义

**Tool 名称**：`list_pets`

**功能描述**：查询宠物档案列表，支持按多种参数筛选、排序、分页。

### 输入 Schema

```python
class ListPetsInput(BaseModel):
    species: str | None = Field(default=None, description="按种类筛选（犬、猫、兔等12种）")
    doctor: str | None = Field(default=None, description="按主治医生筛选（12位医生）")
    status: str | None = Field(default=None, description="按就诊状态筛选（待就诊/就诊中/住院中/已康复/慢性病随访）")
    keyword: str | None = Field(default=None, description="关键词全文搜索（跨字段，含病历全文）")
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数")
    sort_by: str | None = Field(default=None, description="排序字段（name/totalCost/visitCount/ageMonths等）")
    order: str | None = Field(default=None, description="排序方向（asc 或 desc）")
    min_cost: float | None = Field(default=None, ge=0, description="最低总花费区间")
    max_cost: float | None = Field(default=None, ge=0, description="最高总花费区间")
```

### 输出 Schema

```python
class PetSummary(BaseModel):
    id: str                  # 档案ID，如 "PET-000001"
    name: str                # 宠物姓名
    species: str             # 种类
    breed: str               # 品种
    doctor: str              # 主治医生
    status: str              # 就诊状态
    total_cost: float        # 总花费 = charges[].amount 之和
    visit_count: int         # 就诊次数 = records.length

class ListPetsOutput(BaseModel):
    total: int               # 符合条件总数
    page: int                # 当前页码
    page_size: int           # 每页条数
    pets: list[PetSummary]   # 宠物列表（分页结果）
```

---

## 四、技术栈

| 组件 | 选择 |
|---|---|
| 语言 | Python 3.10+ |
| MCP SDK | `mcp`（v2.x，`pip install mcp`），实现 2026-07-28 协议 |
| 数据验证 | `pydantic`（`BaseModel` + `Field`，由 SDK 自动生成 JSON Schema） |
| 数据库 | 直接读取 `data/pet.db`（复用原有嵌入式存储引擎） |
| 传输 | Stdio（默认）+ Streamable HTTP（通过 `MCP_TRANSPORT=http` 切换） |

### Python SDK 核心 API 参考

```python
from mcp.server import MCPServer

# 创建服务器实例（必须包含 Implementation 信息，用于 server/discover）
server = MCPServer(
    name="pet-hospital",
    version="1.0.0",
)

# 注册 Tool（使用 @server.tool 装饰器，类型提示自动推断 JSON Schema）
@server.tool(name="list_pets", description="查询宠物档案列表")
async def list_pets(...) -> dict:
    ...

# 启动传输
from mcp.server.stdio import StdioServerTransport
transport = StdioServerTransport()
await server.run(transport)

# 或 Streamable HTTP
from mcp.server.streamable_http import StreamableHTTPServerTransport
transport = StreamableHTTPServerTransport()
await server.run(transport, host="127.0.0.1", port=8080)
```

---

## 五、代码结构

```
pet-hospital-mcp/
├── main.py               # 入口：创建 server，注册 tool，选择 transport
├── requirements.txt      # 依赖声明
├── internal/
│   ├── store.py          # 数据库读取层（解析 pet.db）
│   ├── models.py         # Pet, Record, Charge 数据模型（Pydantic）
│   └── api_client.py     # HTTP 客户端（转发到 pethospital.exe 的 REST API，可选方案）
└── data/
    └── pet.db            # 数据库文件（从原项目复制）
```

---

## 六、实现指导

### 6.1 入口文件 `main.py`

```python
import asyncio
import os
from mcp.server import MCPServer
from mcp.server.stdio import StdioServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport

# 导入工具注册
from internal.tools import register_tools

server = MCPServer(
    name="pet-hospital",
    version="1.0.0",
    # 2026-07-28 协议：serverInfo 会在每个响应的 _meta 中自动返回
)

register_tools(server)

async def main():
    # 传输选择：MCP_TRANSPORT=http 时使用 Streamable HTTP，否则 Stdio
    if os.getenv("MCP_TRANSPORT") == "http":
        transport = StreamableHTTPServerTransport()
        await server.run(transport, host="127.0.0.1", port=8080)
    else:
        transport = StdioServerTransport()
        await server.run(transport)

if __name__ == "__main__":
    asyncio.run(main())
```

### 6.2 工具注册 `internal/tools.py`

```python
from mcp.server.models import CacheableResult
from internal.store import PetStore

store = PetStore()  # 全局数据库实例，启动时加载 pet.db

def register_tools(server):
    server.tool(name="list_pets", description="查询宠物档案列表，支持按种类/医生/状态筛选、关键词搜索、排序和分页")(list_pets)

async def list_pets(
    species: str | None = None,
    doctor: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str | None = None,
    order: str | None = None,
    min_cost: float | None = None,
    max_cost: float | None = None,
) -> dict:
    """
    查询宠物档案列表。

    实现步骤：
    1. 从 PetStore 获取所有宠物数据
    2. 根据参数过滤：
       - species: 精确匹配
       - doctor: 精确匹配
       - status: 精确匹配
       - keyword: 全文搜索，跨 name/species/breed/doctor/disease/ownerName 等字段
    3. 排序：sort_by + order（默认按 id 升序）
    4. 分页：page + page_size
    5. 计算每个 Pet 的 total_cost 和 visit_count（如果不在 store 中预计算）
    6. 返回 {total, page, page_size, pets: [...]}

    输出必须包含 resultType: "complete"
    """
    # TODO: 实现数据库读取和过滤逻辑
    ...
```

### 6.3 数据库读取层 `internal/store.py`

```python
import struct
import zlib
import json
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

class Pet(BaseModel):
    id: str
    name: str
    species: str
    breed: str
    gender: str
    ageMonths: int
    color: str
    chipId: Optional[str]
    ownerName: str
    ownerPhone: str
    address: str
    doctor: str
    disease: str
    status: str
    allergy: str
    records: list[dict]  # Record 列表
    charges: list[dict]  # Charge 列表
    totalCost: float = 0.0
    visitCount: int = 0

class PetStore:
    """解析 pet.db 文件，维护内存索引。"""

    def __init__(self, db_path: str = "./data/pet.db"):
        self.db_path = db_path
        self.pets: dict[str, Pet] = {}
        self._load()

    def _load(self):
        """读取 pet.db，验证 magic，回放 Record 重建内存索引。"""
        with open(self.db_path, "rb") as f:
            # 读取 Header 32B
            header = f.read(32)
            magic = header[:12].decode().strip('\x00')
            if magic != "PETDBv1":
                raise ValueError(f"Unknown database format: {magic}")
            version = struct.unpack('<I', header[12:16])[0]
            flags = struct.unpack('<I', header[16:20])[0]

            # 回放 Record 构建内存索引
            while True:
                raw_len = f.read(4)
                if not raw_len or len(raw_len) < 4:
                    break
                rec_len = struct.unpack('<I', raw_len)[0]
                crc = struct.unpack('<I', f.read(4))[0]
                payload = f.read(rec_len)

                # CRC32 校验
                if zlib.crc32(payload) != crc:
                    # 损坏自愈：截断到最后一个完整记录
                    break

                rec = json.loads(payload.decode('utf-8'))
                if rec.get("op") == "put":
                    pet = Pet(**rec["data"])
                    # 计算派生字段
                    pet.totalCost = sum(c.get("amount", 0) for c in pet.charges)
                    pet.visitCount = len(pet.records)
                    self.pets[pet.id] = pet
                elif rec.get("op") == "del":
                    self.pets.pop(rec["id"], None)

    def list(self, **filters) -> list[Pet]:
        """返回所有宠物，支持过滤/排序/分页。"""
        results = list(self.pets.values())
        # 过滤逻辑
        if filters.get("species"):
            results = [p for p in results if p.species == filters["species"]]
        if filters.get("doctor"):
            results = [p for p in results if p.doctor == filters["doctor"]]
        if filters.get("status"):
            results = [p for p in results if p.status == filters["status"]]
        if filters.get("keyword"):
            kw = filters["keyword"].lower()
            results = [p for p in results if any(
                kw in str(getattr(p, f, "")).lower()
                for f in ["name", "species", "breed", "doctor", "disease", "ownerName"]
            )]
        if filters.get("min_cost") is not None:
            results = [p for p in results if p.totalCost >= filters["min_cost"]]
        if filters.get("max_cost") is not None:
            results = [p for p in results if p.totalCost <= filters["max_cost"]]
        # 排序
        sort_by = filters.get("sort_by", "id")
        order = filters.get("order", "asc")
        reverse = order == "desc"
        results.sort(key=lambda p: getattr(p, sort_by, p.id), reverse=reverse)
        return results

    def count(self, **filters) -> int:
        return len(self.list(**filters))
```

### 6.4 依赖声明 `requirements.txt`

```
mcp>=2.0.0
pydantic>=2.0.0
```

---

## 七、启动与运行

```bash
# 创建项目目录
mkdir pet-hospital-mcp && cd pet-hospital-mcp

# 安装依赖
pip install mcp pydantic

# 复制数据库文件
cp ../windows/data/pet.db ./data/

# Stdio 模式（默认，MCP Inspector 推荐）
python main.py

# Streamable HTTP 模式
MCP_TRANSPORT=http python main.py
# 然后访问 http://127.0.0.1:8080/mcp

# 使用 MCP Inspector 测试
npx @modelcontextprotocol/inspector python main.py

# 或使用 Python SDK 的 InMemoryTransport 单元测试
```

---

## 八、Streamable HTTP 传输配置

使用 `MCP_TRANSPORT=http` 时，服务器以 Streamable HTTP 模式运行：

- 请求必须包含 `Mcp-Method` 和 `Mcp-Name` 头
- `_meta` 必须包含 `io.modelcontextprotocol/protocolVersion: "2026-07-28"`
- 响应 `_meta` 必须包含 `io.modelcontextprotocol/serverInfo`
- SDK 自动处理头与 body 的一致性校验
- 服务器必须实现 `server/discover`（Python SDK v2 自动注册）

---

## 九、关键注意事项

1. **禁止使用已废弃功能**：不实现 `initialize`/`initialized` 握手、不依赖 session、不使用 Roots/Sampling/Logging/HTTP+SSE、DCR、`ping`
2. **`server/discover`**：Python SDK v2 自动注册，确保 `MCPServer(name, version)` 正确配置
3. **`_meta` 字段**：每个请求的 `_meta` 包含 `protocolVersion` 和 `clientCapabilities`，每个响应的 `_meta` 包含 `serverInfo`
4. **`CacheableResult`**：`tools/list` 结果必须设置 `ttlMs`（建议 60000）和 `cacheScope: "private"`
5. **工具列表排序**：按名称字典序排列，确保确定性顺序
6. **`resultType` 字段**：每个结果必须包含 `resultType: "complete"`
7. **错误码**：遵循 2026-07-28 分配（`-32020` 到 `-32099` 为规范保留）
8. **数据库路径**：默认 `./data/pet.db`，可通过环境变量 `PET_DB_PATH` 配置
9. **MRTR 支持**：如果将来需要用户确认的操作（如删除），返回 `resultType: "input_required"`

---

## 十、测试验证清单

1. **`server/discover`**：返回正确的协议版本 `2026-07-28` 和能力列表
2. **`tools/list`**：包含 `list_pets` 工具，按名称排序，含 `ttlMs` 和 `cacheScope`
3. **`_meta` 字段**：`tools/list` 结果的 `_meta` 含 `serverInfo`
4. **`list_pets` 功能**：
   - 无参数：返回全部宠物（分页）
   - `species=犬`：仅返回犬类
   - `doctor=李医生`：仅返回该医生的宠物
   - `keyword=肠胃炎`：全文搜索匹配
   - `min_cost=1000&max_cost=5000`：按总花费区间筛选
   - `sort_by=totalCost&order=desc`：按总花费降序
   - `page=2&page_size=10`：正确分页
   - 组合筛选：多种参数同时生效
5. **Streamable HTTP**：`Mcp-Method`/`Mcp-Name` 头与 body 一致
6. **无废弃功能**：确认无 `initialize`、`ping`、`logging/setLevel`、session 相关代码
7. **数据库读取**：正确解析 `data/pet.db` 的 1008 条数据
8. **CRC 校验**：损坏记录的截断修复逻辑正常
9. **优雅退出**：`Ctrl+C` 安全退出

---

## 十一、后续扩展计划

MVP 完成后，按以下顺序扩展：

1. `get_pet` — 按 ID 查询单个档案（含病历和消费明细）
2. `create_pet` — 新增宠物档案
3. `search_pets` — 全文检索
4. `get_records` / `add_record` — 病历管理
5. `get_charges` / `add_charge` — 消费明细
6. `get_stats` — 经营统计
7. `batch_create` / `batch_delete` — 批量操作
8. `export_data` — 数据导出

---

*请严格按照以上规范开发，确保完全兼容 MCP 2026-07-28 协议。*
