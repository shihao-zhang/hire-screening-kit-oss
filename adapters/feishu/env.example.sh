# 私有实例环境变量示例——复制到 kit 仓外(如 <workspace>/config/feishu.env),填真值后 source。
# ⚠️ 真实 env 文件含 token,永不提交进本仓。
export HSK_WORKSPACE=/path/to/your-workspace
export HSK_CONFIG=/path/to/your-job.toml
export HSK_KIT=/path/to/hire-screening-kit
export HSK_BASE_TOKEN=your_bitable_app_token
export HSK_TABLE_ID=tblxxxxxxxx
# 系统 python3 低于 3.11(无 tomllib)时,指定解释器(可含参数,如 uv):
export HSK_PYTHON="uv run python3"
