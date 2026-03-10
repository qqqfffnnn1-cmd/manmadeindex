import os
import requests
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# 复用 TK_StockRelation 的代理绕过模式
def make_session():
    session = requests.Session()
    session.trust_env = False
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
        os.environ.pop(key, None)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://finance.qq.com/',
    })
    return session

SESSION = make_session()

def code_to_symbol(code: str) -> str:
    """股票代码转腾讯格式"""
    code = code.strip()
    if code.startswith(('sh', 'sz', 'hk')):
        return code
    if code.startswith(('6', '9')):
        return f'sh{code}'
    return f'sz{code}'

def symbol_to_em_secid(code: str) -> str:
    """股票代码转东方财富 secid 格式"""
    code = code.strip()
    if code.startswith(('6', '9')):
        return f'1.{code}'
    return f'0.{code}'

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search')
def search_stock():
    """东方财富搜索股票（支持代码/名称/拼音）"""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    try:
        url = (f'https://searchapi.eastmoney.com/api/suggest/get'
               f'?input={requests.utils.quote(q)}&type=14'
               f'&token=D43BF722C8E33BDC906FB84D85E326E8&count=20')
        resp = SESSION.get(url, timeout=8)
        data = resp.json()
        items = data.get('QuotationCodeTable', {}).get('Data', [])
        results = []
        for item in items:
            code = item.get('Code', '')
            name = item.get('Name', '')
            classify = item.get('Classify', '')
            # 只保留A股，6位纯数字代码
            if classify == 'AStock' and len(code) == 6 and code.isdigit():
                results.append({'code': code, 'name': name})
        return jsonify(results[:10])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/kline')
def get_kline():
    """获取单只股票K线（调试用）"""
    code = request.args.get('code', '')
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    data = fetch_kline(code, start, end)
    return jsonify(data)

def fetch_kline(code: str, start_date: str, end_date: str) -> dict:
    """拉取腾讯复权K线，返回 {date, open, close, high, low, volume}"""
    symbol = code_to_symbol(code)
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={symbol},day,{start_date},{end_date},500,qfq')
    try:
        resp = SESSION.get(url, timeout=10)
        data = resp.json()
        raw = data.get('data', {}).get(symbol, {})
        # 优先取 qfqday，其次 day
        bars = raw.get('qfqday') or raw.get('day') or []
        result = []
        for bar in bars:
            if len(bar) >= 6:
                result.append({
                    'date': bar[0],
                    'open': float(bar[1]),
                    'close': float(bar[2]),
                    'high': float(bar[3]),
                    'low': float(bar[4]),
                    'volume': float(bar[5]),
                })
        return {'code': code, 'bars': result}
    except Exception as e:
        return {'code': code, 'bars': [], 'error': str(e)}

def fetch_weight_data(codes: list) -> dict:
    """东方财富API获取市值和成交额"""
    secids = ','.join(symbol_to_em_secid(c) for c in codes)
    url = (f'http://push2.eastmoney.com/api/qt/ulist.np/get'
           f'?fltt=2&invt=2&fields=f12,f14,f20,f6&secids={secids}')
    weights = {}
    try:
        resp = SESSION.get(url, timeout=10)
        data = resp.json()
        diff = data.get('data', {}).get('diff', [])
        for item in diff:
            code = str(item.get('f12', ''))
            market_cap = item.get('f20', 0) or 0   # 总市值
            amount = item.get('f6', 0) or 0         # 成交额
            if code:
                weights[code] = {
                    'market_cap': float(market_cap),
                    'amount': float(amount),
                }
    except Exception as e:
        print(f'[weight] error: {e}')
    return weights

@app.route('/api/build_index', methods=['POST'])
def build_index():
    """构建自定义指数"""
    body = request.get_json()
    codes = body.get('codes', [])
    weight_type = body.get('weight_type', 'equal')  # equal / market_cap / amount
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')

    if not codes:
        return jsonify({'error': '请至少选择一只股票'}), 400
    if len(codes) > 30:
        return jsonify({'error': '最多支持30只股票'}), 400

    # 默认时间范围：近1年
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

    # 并发拉取K线
    kline_data = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_kline, code, start_date, end_date): code for code in codes}
        for future in as_completed(futures):
            result = future.result()
            code = result['code']
            kline_data[code] = result['bars']

    # 过滤掉没有数据的股票
    valid_codes = [c for c in codes if kline_data.get(c)]
    if not valid_codes:
        return jsonify({'error': '所有股票数据获取失败，请检查网络'}), 500

    # 获取权重数据
    weight_map = {}
    if weight_type in ('market_cap', 'amount'):
        raw_weights = fetch_weight_data(valid_codes)
        total = 0
        for code in valid_codes:
            val = raw_weights.get(code, {}).get(weight_type, 0)
            weight_map[code] = val
            total += val
        if total > 0:
            for code in valid_codes:
                weight_map[code] = weight_map[code] / total
        else:
            # 降级为等额
            for code in valid_codes:
                weight_map[code] = 1.0 / len(valid_codes)
    else:
        for code in valid_codes:
            weight_map[code] = 1.0 / len(valid_codes)

    # 构建日期对齐的价格矩阵
    # 找所有股票共同的交易日
    date_sets = [set(b['date'] for b in kline_data[c]) for c in valid_codes]
    common_dates = sorted(date_sets[0].intersection(*date_sets[1:]))

    if len(common_dates) < 2:
        # 如果共同日期太少，用并集，缺失用前值填充
        all_dates = sorted(set().union(*date_sets))
        common_dates = all_dates

    # 构建每只股票的日期->bar映射
    bar_maps = {}
    for code in valid_codes:
        bar_maps[code] = {b['date']: b for b in kline_data[code]}

    # 前向填充缺失数据
    def fill_forward(code, dates):
        bmap = bar_maps[code]
        filled = {}
        last = None
        for d in dates:
            if d in bmap:
                last = bmap[d]
            if last:
                filled[d] = last
        return filled

    filled_maps = {code: fill_forward(code, common_dates) for code in valid_codes}

    # 计算指数K线
    index_bars = []
    base_value = 1000.0
    prev_close_index = base_value

    for i, date in enumerate(common_dates):
        # 收集当日各股数据
        day_data = {}
        for code in valid_codes:
            bar = filled_maps[code].get(date)
            if bar:
                day_data[code] = bar

        if not day_data:
            continue

        if i == 0:
            # 基准日：指数=1000，OHLC都是1000
            index_bars.append({
                'date': date,
                'open': base_value,
                'close': base_value,
                'high': base_value,
                'low': base_value,
            })
            prev_close_index = base_value
            # 记录基准价格
            base_prices = {code: day_data[code]['close'] for code in day_data}
            continue

        # 计算加权收益率
        w_open_ret = 0.0
        w_close_ret = 0.0
        w_high_ret = 0.0
        w_low_ret = 0.0
        total_w = 0.0

        for code in valid_codes:
            bar = day_data.get(code)
            if not bar:
                continue
            base_price = base_prices.get(code, bar['close'])
            if base_price == 0:
                continue
            w = weight_map.get(code, 0)
            prev_bar = filled_maps[code].get(common_dates[i-1])
            prev_close = prev_bar['close'] if prev_bar else base_price

            if prev_close == 0:
                continue

            open_ret = (bar['open'] - prev_close) / prev_close
            close_ret = (bar['close'] - prev_close) / prev_close
            high_ret = (bar['high'] - prev_close) / prev_close
            low_ret = (bar['low'] - prev_close) / prev_close

            w_open_ret += w * open_ret
            w_close_ret += w * close_ret
            w_high_ret += w * high_ret
            w_low_ret += w * low_ret
            total_w += w

        if total_w > 0:
            w_open_ret /= total_w
            w_close_ret /= total_w
            w_high_ret /= total_w
            w_low_ret /= total_w

        open_idx = prev_close_index * (1 + w_open_ret)
        close_idx = prev_close_index * (1 + w_close_ret)
        high_idx = prev_close_index * (1 + w_high_ret)
        low_idx = prev_close_index * (1 + w_low_ret)

        # 确保 high >= max(open, close), low <= min(open, close)
        high_idx = max(high_idx, open_idx, close_idx)
        low_idx = min(low_idx, open_idx, close_idx)

        index_bars.append({
            'date': date,
            'open': round(open_idx, 2),
            'close': round(close_idx, 2),
            'high': round(high_idx, 2),
            'low': round(low_idx, 2),
        })
        prev_close_index = close_idx

    # 返回结果
    return jsonify({
        'bars': index_bars,
        'stocks': [
            {
                'code': code,
                'weight': round(weight_map.get(code, 0) * 100, 2),
            }
            for code in valid_codes
        ],
        'weight_type': weight_type,
        'start_date': start_date,
        'end_date': end_date,
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003, debug=True)
