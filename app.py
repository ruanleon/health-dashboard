import streamlit as st
from openai import OpenAI
from PIL import Image
import io
import base64
import re
import os
import json
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# 0. 环境配置
load_dotenv()
st.set_page_config(page_title="Health Dashboard Pro", layout="wide", page_icon="🫀")

# --- 隐藏式配置读取 ---
if "POIXE_API_KEY" in st.secrets:
    api_key = st.secrets["POIXE_API_KEY"]
    api_status = "✅ API Key 已配置"
else:
    api_key = os.getenv("POIXE_API_KEY", "")
    api_status = "⚠️ 未检测到 Secrets API Key"

if "spreadsheet_url" in st.secrets:
    SHEET_URL = st.secrets["spreadsheet_url"]
    sheet_status = "✅ Google Sheet 连接就绪"
else:
    SHEET_URL = ""
    sheet_status = "⚠️ 未配置 Google Sheet URL"

# 1. 核心工具函数
def smart_process_image(uploaded_file):
    uploaded_file.seek(0)
    file_bytes = uploaded_file.getvalue()
    if len(file_bytes) / 1024 < 500:
        return file_bytes, "image/jpeg"

    image = Image.open(uploaded_file)
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
        
    filename = uploaded_file.name
    is_screenshot = any(k in filename for k in ["Screenshot", "SHealth", "ReactNative", "屏幕截图"])
    
    buffer = io.BytesIO()
    if is_screenshot:
        image.save(buffer, format="JPEG", quality=95)
    else:
        target_width = 2048
        if image.width > target_width:
            ratio = target_width / image.width
            new_height = int(image.height * ratio)
            image = image.resize((target_width, new_height), Image.Resampling.LANCZOS)
        image.save(buffer, format="JPEG", quality=75)
    return buffer.getvalue(), "image/jpeg"

def parse_file_info(filename):
    """
    文件名解析逻辑
    """
    # 1. 显式关键字匹配
    if "ReactNative" in filename or "Screenshot" in filename or "屏幕截图" in filename:
        return None, 'workout_snapshot'
    if "SHealth" in filename:
        return None, 's_health'
    
    # 2. 纯数字文件名匹配 (如 1769760746481.jpg)
    if re.match(r'^\d{13}\.', filename):
        return None, 's_health'
    
    # 3. 日期匹配 (YYYYMMDD)
    match_full = re.search(r'(20\d{2})(\d{2})(\d{2})_(\d{6})', filename)
    if match_full:
        try:
            y, m, d, t = match_full.groups()
            dt_obj = datetime.strptime(f"{y}{m}{d}{t}", "%Y%m%d%H%M%S")
            return dt_obj, 'food'
        except:
            pass

    # 4. 时间匹配 (Fallback)
    match_time = re.search(r'_(\d{6})', filename)
    if match_time:
        try:
            t_str = match_time.group(1)
            if int(t_str) < 240000:
                now = datetime.now()
                t_obj = datetime.strptime(t_str, "%H%M%S").time()
                return datetime.combine(now.date(), t_obj), 'food'
        except:
            pass
            
    return None, 'food'

def extract_json_from_response(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return {}

def normalize_data(data, target_date=None):
    if target_date:
        current_dt = target_date
    else:
        current_dt = datetime.now()
        
    week_map = {0:"一", 1:"二", 2:"三", 3:"四", 4:"五", 5:"六", 6:"日"}
    data['日期'] = current_dt.strftime("%Y-%m-%d")
    data['星期'] = f"周{week_map[current_dt.weekday()]}"
    
    default_schema = {
        "营养摄入汇总": {
            "总热量": 0, "总蛋白质": 0, "总碳水": 0, "总脂肪": 0, "总膳食纤维": 0, 
            "总盈余缺口分析": "暂无分析"
        },
        "早餐": {"时间": "N/A", "内容": "", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": ""},
        "午餐": {"时间": "N/A", "内容": "", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": ""},
        "晚餐": {"时间": "N/A", "内容": "", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": ""},
        "加餐": {"时间": "N/A", "内容": "", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": ""},
        "睡眠": {
            "入睡时间": "N/A", "起床时间": "N/A", "睡眠总时长": "0h", 
            "睡眠阶段分析": "暂无数据", "睡眠点评": ""
        },
        "心率": {
            "静息心率": 0, "平均静息范围": "N/A", "全天心率范围": "N/A", 
            "心率时序分析": "暂无数据", "心率点评": ""
        },
        "压力": {
            "压力均值": 0, "压力时序分析": "暂无数据", "压力点评": "",
        },
        "全天消耗与活动": {
            "总步数": 0, "活动时长": "0min", "活动卡路里": 0, "燃烧的卡路里总数": 0
        },
        "力量训练": {
            "力量主题": "休息日", "具体时间": "N/A", "训练时长": "0min", 
            "总容量": 0, "消耗估算": 0, "力量点评": "",
            "动作流水明细": []
        },
        "有氧训练": {
            "有氧类型": "无", "具体时间": "N/A", "距离": "0km", "有氧时长": "0min", 
            "平均心率": 0, "平均步频": 0, "平均步速": "N/A", "有氧卡路里消耗": 0
        },
        "本日总结": {"本日分析": "", "指导建议": ""}
    }

    for k, v in default_schema.items():
        if k not in data:
            data[k] = v
        elif isinstance(v, dict):
            for sub_k, sub_v in v.items():
                if sub_k not in data[k]:
                    data[k][sub_k] = sub_v
                    
    return data

def save_data_to_gsheet(data, sheet_url):
    row = []
    row.append(data.get('日期'))
    row.append(data.get('星期'))
    
    summ = data.get('营养摄入汇总', {})
    row.extend([
        summ.get('总热量'), summ.get('总蛋白质'), summ.get('总碳水'), 
        summ.get('总脂肪'), summ.get('总膳食纤维'), summ.get('总盈余缺口分析')
    ])
    
    meals = ['早餐', '午餐', '晚餐', '加餐']
    for m in meals:
        meal = data.get(m, {})
        row.extend([
            meal.get('时间'), meal.get('内容'), meal.get('热量'),
            meal.get('蛋白质'), meal.get('碳水'), meal.get('脂肪'),
            meal.get('膳食纤维'), meal.get('点评')
        ])
        
    slp = data.get('睡眠', {})
    row.extend([
        slp.get('入睡时间'), slp.get('起床时间'), slp.get('睡眠总时长'),
        slp.get('睡眠阶段分析'), slp.get('睡眠点评')
    ])
    
    hr = data.get('心率', {})
    row.extend([
        hr.get('静息心率'), hr.get('平均静息范围'), hr.get('全天心率范围'),
        hr.get('心率时序分析'), hr.get('心率点评')
    ])
    
    stres = data.get('压力', {})
    row.extend([
        stres.get('压力均值'), stres.get('压力时序分析'), stres.get('压力点评')
    ])
    
    act = data.get('全天消耗与活动', {})
    row.extend([
        act.get('总步数'), act.get('活动时长'), act.get('活动卡路里'), act.get('燃烧的卡路里总数')
    ])
    
    stren = data.get('力量训练', {})
    details = stren.get('动作流水明细', [])
    details_str = ""
    if isinstance(details, list):
        details_list = [f"{d.get('动作名称','')}({d.get('重量','')}kg*{d.get('次数','')})" for d in details]
        details_str = " | ".join(details_list)
        
    row.extend([
        stren.get('力量主题'), stren.get('具体时间'), stren.get('训练时长'),
        details_str, 
        stren.get('总容量'), stren.get('消耗估算'), stren.get('力量点评')
    ])
    
    cardio = data.get('有氧训练', {})
    row.extend([
        cardio.get('有氧类型'), cardio.get('具体时间'), cardio.get('距离'),
        cardio.get('有氧时长'), cardio.get('平均心率'), cardio.get('平均步频'),
        cardio.get('平均步速'), cardio.get('有氧卡路里消耗')
    ])
    
    summ_txt = data.get('本日总结', {})
    row.extend([
        summ_txt.get('本日分析'), summ_txt.get('指导建议')
    ])

    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            client = gspread.authorize(creds)
            
            try:
                sheet = client.open_by_url(sheet_url).sheet1
            except gspread.SpreadsheetNotFound:
                return False, "找不到表格，请检查 URL 或权限"
                
            sheet.append_row(row)
            return True, "写入成功"
        else:
            return False, "未配置 Google Service Account 凭证"
    except Exception as e:
        return False, str(e)


# 2. Payload 构建
def build_payload(uploaded_files, quick_adds):
    timeline_fixed = []   
    timeline_float = []   
    valid_dates = [] 
    
    with st.status("正在处理图像...", expanded=False) as status:
        for file in uploaded_files:
            b64_str, mime = smart_process_image(file)
            b64_encoded = base64.b64encode(b64_str).decode('utf-8')
            
            file_dt, file_type = parse_file_info(file.name)
            
            item = {"type": "image", "name": file.name, "data": b64_encoded, "mime": mime, "file_type": file_type}
            
            if file_type == 'food':
                if file_dt:
                    item['time'] = file_dt
                    timeline_fixed.append(item)
                    if file_dt.year > 2000:
                        valid_dates.append(file_dt)
                else:
                    item['label'] = "【未归档食物】"
                    timeline_float.append(item)
            elif file_type == 'workout_snapshot':
                item['label'] = "【健身详情截图】"
                timeline_float.append(item)
                if file_dt and file_dt.year > 2000:
                    valid_dates.append(file_dt)
            elif file_type == 's_health':
                item['label'] = "【SHealth汇总】"
                timeline_float.append(item)
                    
        status.update(label="图像处理完成", state="complete")

    if valid_dates:
        report_date = min(valid_dates)
    else:
        report_date = datetime.now()

    timeline_fixed.sort(key=lambda x: x['time'])

    user_content = []
    user_content.append({"type": "text", "text": "## Part 1: 饮食照片流\n(请对以下食物照片进行精确视觉估算，包含热量, 蛋白质, 碳水, 脂肪, 膳食纤维)\n"})
    for item in timeline_fixed:
        t = item['time'].strftime("%H:%M")
        if item.get('type') == 'text':
            user_content.append({"type": "text", "text": f"- {t} {item.get('content')}"})
        else:
            user_content.append({"type": "text", "text": f"- {t} [食物照片] (请估算热量及宏量营养素)"})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:{item['mime']};base64,{item['data']}"}})

    supplement_text = ""
    if quick_adds.get('bcaa'): supplement_text += "- BCAA 6g (训练中摄入)\n"
    if quick_adds.get('protein'): supplement_text += "- 蛋白粉 32g + 肌酸 3g (训练后摄入)\n"
    if supplement_text:
        user_content.append({
            "type": "text", 
            "text": f"\n## 特别指令：补剂\n【强制要求】请将以下补剂合并计算入 JSON 的 `加餐` 字段：\n{supplement_text}"
        })

    imgs = [x for x in timeline_float if x['file_type'] in ['workout_snapshot', 's_health']]
    if imgs:
        user_content.append({"type": "text", "text": "\n## Part 2: 健康数据截图 (OCR)\n请提取包括步频、配速、压力时序等所有详细数据。\n"})
        for img in imgs:
            user_content.append({"type": "text", "text": f"📸 {img['label']}"})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:{img['mime']};base64,{img['data']}"}})
            
    return user_content, report_date

# 3. JSON Schema
RESPONSE_SCHEMA = """
{
  "营养摄入汇总": {
    "总热量": 0, "总蛋白质": 0, "总碳水": 0, "总脂肪": 0, "总膳食纤维": 0,
    "总盈余缺口分析": "..."
  },
  "早餐": { 
    "时间": "HH:MM", "内容": "...", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": "..." 
  },
  "午餐": { 
    "时间": "HH:MM", "内容": "...", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": "..." 
  },
  "晚餐": { 
    "时间": "HH:MM", "内容": "...", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": "..." 
  },
  "加餐": { 
    "时间": "HH:MM", "内容": "...", "热量": 0, "蛋白质": 0, "碳水": 0, "脂肪": 0, "膳食纤维": 0, "点评": "..." 
  },
  "睡眠": { 
    "入睡时间": "HH:MM", "起床时间": "HH:MM", "睡眠总时长": "...", 
    "睡眠阶段分析": "...", "睡眠点评": "..." 
  },
  "心率": { 
    "静息心率": 0, "平均静息范围": "...", "全天心率范围": "...", 
    "心率时序分析": "...", "心率点评": "..." 
  },
  "压力": { 
    "压力均值": 0, "压力时序分析": "...", "压力点评": "..." 
  },
  "全天消耗与活动": { 
    "总步数": 0, "活动时长": "...", "活动卡路里": 0, "燃烧的卡路里总数": 0 
  },
  "力量训练": {
    "力量主题": "...", "具体时间": "HH:MM", "训练时长": "...",
    "动作流水明细": [ 
      { 
        "动作名称": "...", "OCR原始行": "如: 1/热 10+10kg 12", "组序号": "1", "重量": 20, "次数": 12 
      } 
    ],
    "总容量": 0, "消耗估算": 0, "力量点评": "..."
  },
  "有氧训练": { 
    "有氧类型": "...", "具体时间": "HH:MM", "距离": "...", "有氧时长": "...", 
    "平均心率": "...", "平均步频": "...", "平均步速": "...", "有氧卡路里消耗": "..." 
  },
  "本日总结": { "本日分析": "...", "指导建议": "..." }
}
"""

# 4. UI 主程序

with st.sidebar:
    st.markdown("⚙️ **系统状态**")
    st.caption(f"API Connection: {api_status}")
    st.caption(f"Storage: {sheet_status}")
    
    st.divider()
    st.markdown("💾 **设置**")
    auto_save = st.checkbox("自动同步到 Google Sheets", value=True, disabled=(SHEET_URL==""))

uploaded_files = st.file_uploader("📤 **上传记录 (截图/食物)**", accept_multiple_files=True, type=['jpg', 'jpeg', 'png'])

# 快速补剂移至主页面
qc1, qc2 = st.columns(2)
with qc1:
    opt_bcaa = st.checkbox(''':blue-background[🥤 练中 BCAA]''')
with qc2:
    opt_protein = st.checkbox(''':blue-background[🥛 练后 蛋白粉]''')
quick_adds = {"bcaa": opt_bcaa, "protein": opt_protein}

if st.button("🚀 生成详细报告", type="primary"):
    if not uploaded_files:
        st.warning("请上传图片")
        st.stop()
    if not api_key:
        st.error("未检测到 API Key，请检查 secrets.toml 配置")
        st.stop()
        
    try:
        user_content, report_date = build_payload(uploaded_files, quick_adds)
        
        system_prompt = f"""你是一名精英营养师和数据分析师。
        
        【任务 1：力量训练 - 逐行提取】
        **不要合并！** 截图有几组，数组里就有几个对象。
        **不要乘序号！** 单组容量 = 重量 * 次数。
        
        【任务 2：膳食纤维与营养】
        对食物照片进行估算时，必须进行精确视觉估算，包含热量, 蛋白质, 碳水, 脂肪, 膳食纤维数据。
        
        【任务 3：压力均值】
        若无直接均值，按 (高*90 + 中*65 + 低*40 + 放松*10)/100 计算。

        【输出要求】
        严格 JSON 格式，不要多余文本。
        {RESPONSE_SCHEMA}
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        client = OpenAI(api_key=api_key, base_url="https://api.poixe.com/v1")
        
        with st.spinner("正在全维度解析..."):
            response = client.chat.completions.create(
                model="gemini-2.0-flash", 
                messages=messages,
                temperature=0.0, 
                response_format={"type": "json_object"}
            )
            
        result_text = response.choices[0].message.content
        raw_data = extract_json_from_response(result_text)
        
        # === 数据归一化 ===
        data = normalize_data(raw_data, target_date=report_date)
        
        # === 力量数据聚合 ===
        workout_df = pd.DataFrame()
        strength_data = data.get('力量训练', {})
        details = strength_data.get('动作流水明细', [])
        
        total_vol = 0
        if details:
            for d in details:
                try:
                    w = float(d.get('重量', 0))
                    r = float(d.get('次数', 0))
                    d['单组容量'] = w * r
                    total_vol += d['单组容量']
                except:
                    d['单组容量'] = 0
            strength_data['总容量'] = total_vol 
            workout_df = pd.DataFrame(details)
        
        # === 状态同步 ===
        st.toast(f"✅ 解析完成 | 日期: {data['日期']}", icon="📅")
        
        if auto_save and SHEET_URL:
            with st.spinner("正在同步到云端..."):
                success, msg = save_data_to_gsheet(data, SHEET_URL)
                if success:
                    st.toast("✅ 数据已同步到 Google Sheet", icon="☁️")
                else:
                    st.error(f"❌ 同步失败: {msg}")
        
        # ==========================================
        # 5. 专业表格化展示 (Mobile Optimized - Direct Display)
        # ==========================================
        
        # --- 核心摘要表 ---
        st.markdown("📊 **每日概览**")
        summary_data = [
            {"指标": "总摄入", "数值": f"{data['营养摄入汇总']['总热量']} kcal", "详情": f"Fib: {data['营养摄入汇总']['总膳食纤维']}g, Pro: {data['营养摄入汇总']['总蛋白质']}g"},
            {"指标": "总消耗", "数值": f"{data['全天消耗与活动']['燃烧的卡路里总数']} kcal", "详情": "包含基础代谢与活动消耗"},
            {"指标": "热量差", "数值": f"{data['营养摄入汇总']['总热量'] - data['全天消耗与活动']['燃烧的卡路里总数']} kcal", "详情": data['营养摄入汇总']['总盈余缺口分析']},
            {"指标": "训练容量", "数值": f"{int(total_vol)} kg", "详情": strength_data.get('力量主题', '休息日')},
            {"指标": "压力均值", "数值": f"{data['压力']['压力均值']}", "详情": data['压力']['压力点评'][:20]+"..."}
        ]
        st.dataframe(pd.DataFrame(summary_data), width="stretch", hide_index=True)

        st.divider()

        # --- 1. 饮食详情 ---
        st.markdown("🍽️ **饮食详情**")
        macros_data = []
        for m in ['早餐', '午餐', '晚餐', '加餐']:
            row = data[m]
            macros_data.append({
                "餐别": m,
                "时间": row['时间'],
                "内容": row['内容'],
                "Cal": row['热量'],
                "P": row['蛋白质'],
                "C": row['碳水'],
                "F": row['脂肪'],
                "Fib": row['膳食纤维']
            })
        df_macros = pd.DataFrame(macros_data)
        st.dataframe(df_macros, width="stretch", hide_index=True)
        st.caption("注: P=蛋白质, C=碳水, F=脂肪, Fib=膳食纤维 (单位:g)")

        st.divider()

        # --- 2. 力量训练 ---
        st.markdown("🏋️ **力量训练**")
        st.markdown(f"**主题: {strength_data.get('力量主题', '无')}**")
        wo_meta = [
            {"项目": "开始时间", "数据": strength_data.get('具体时间')},
            {"项目": "训练时长", "数据": strength_data.get('训练时长')},
            {"项目": "总容量", "数据": f"{total_vol} kg"},
            {"项目": "估算消耗", "数据": f"{strength_data.get('消耗估算')} kcal"}
        ]
        st.dataframe(pd.DataFrame(wo_meta), width="stretch", hide_index=True)
        
        if not workout_df.empty and "动作名称" in workout_df.columns:
            workout_df['组详情'] = workout_df.apply(
                lambda x: f"{x.get('重量',0)}kg×{x.get('次数',0)}", axis=1
            )
            df_agg = workout_df.groupby("动作名称", as_index=False).agg({
                "组详情": lambda x: " | ".join(x),
                "单组容量": "sum",
                "OCR原始行": "count"
            })
            df_agg.columns = ["动作名称", "记录", "总容量", "组数"]
            df_agg = df_agg[["动作名称", "记录"]] 
            st.dataframe(df_agg, width="stretch", hide_index=True)
        
        st.info(f"💡 {strength_data.get('力量点评')}")

        st.divider()

        # --- 3. 有氧训练 ---
        st.markdown("🏃 **有氧训练**")
        st.markdown(f"**项目: {data['有氧训练']['有氧类型']}**")
        ac = data['有氧训练']
        cardio_table = [
            {"指标": "距离", "数值": ac['距离']},
            {"指标": "时长", "数值": ac['有氧时长']},
            {"指标": "配速", "数值": ac['平均步速']},
            {"指标": "平均心率", "数值": f"{ac['平均心率']} bpm"},
            {"指标": "消耗", "数值": f"{ac['有氧卡路里消耗']} kcal"}
        ]
        st.dataframe(pd.DataFrame(cardio_table), width="stretch", hide_index=True)

        st.divider()

        # --- 4. 睡眠与压力 ---
        st.markdown("💤 **睡眠 & 压力**")
        slp = data['睡眠']
        sts = data['压力']
        health_table = [
            {"类别": "睡眠", "指标": "时间", "数值": f"{slp['入睡时间']} - {slp['起床时间']}"},
            {"类别": "睡眠", "指标": "时长", "数值": slp['睡眠总时长']},
            {"类别": "压力", "指标": "均值", "数值": sts['压力均值']},
            {"类别": "压力", "指标": "评价", "数值": sts['压力点评']}
        ]
        st.dataframe(pd.DataFrame(health_table), width="stretch", hide_index=True)
        st.caption(f"睡眠分析: {slp['睡眠阶段分析']}")

        st.divider()

        # --- 5. 心率与活动 ---
        st.markdown("❤️ **心率 & 活动**")
        hr = data['心率']
        act = data['全天消耗与活动']
        body_table = [
            {"类别": "心率", "指标": "静息心率", "数值": f"{hr['静息心率']} bpm"},
            {"类别": "心率", "指标": "全天范围", "数值": hr['全天心率范围']},
            {"类别": "活动", "指标": "总步数", "数值": act['总步数']},
            {"类别": "活动", "指标": "活动热量", "数值": f"{act['活动卡路里']} kcal"}
        ]
        st.dataframe(pd.DataFrame(body_table), width="stretch", hide_index=True)

        st.divider()

        # --- 6. 总结与建议 ---
        st.markdown("📝 **总结与建议**")
        st.markdown("📅 **本日分析**")
        st.write(data['本日总结']['本日分析'])
        st.markdown("🛡️ **指导建议**")
        st.success(data['本日总结']['指导建议'])
        
        with st.expander("查看原始 JSON"):
            st.json(data)
            
    except Exception as e:
        st.error(f"处理过程中发生错误: {e}")