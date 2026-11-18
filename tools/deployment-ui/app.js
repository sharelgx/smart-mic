const token = document.querySelector('meta[name=smart-mic-session]').content;
const form = document.querySelector('#form');
const title = document.querySelector('#title');
const instructions = document.querySelector('#instructions');
const fields = document.querySelector('#fields');
const status = document.querySelector('#status');
const action = document.querySelector('#action');
const close = document.querySelector('#close');
const steps = [
  {
    label: '检查 Mac', title: '先准备这台 Mac', button: '检查并准备', endpoint: 'setup',
    copy: '<p>部署程序会为 Smart Mic 准备独立运行环境。第一次可能需要几分钟。</p><ul><li>安装 <a href="https://www.python.org/downloads/macos/" target="_blank" rel="noreferrer">Python 3.12</a>、Codex 桌面应用和搜狗语音助手。</li><li>安装 BlackHole 2ch，在「系统设置 → 声音 → 输入」选中它。</li><li>让 Mac 连上设备要使用的局域网。</li></ul>',
  },
  {
    label: '连接设备', title: '用 USB 连接 Smart Mic', button: '检测设备', endpoint: 'device',
    copy: '<img class="device" src="/logo.png" alt="Smart Mic 的板端界面"><p>用一根能传数据的 USB 线连接 FoloToy AI Passport。请一次只连接一台设备。</p><p>如果点检测后找不到设备，试着换一根 USB 线或换一个接口。</p>',
  },
  {
    label: '配对 Wi-Fi', title: '让设备加入 Wi-Fi', button: '开始配对', endpoint: 'pair',
    copy: '<p>填写设备可连接的 2.4 GHz Wi-Fi。密码只会写到设备，用于它重新连接网络。</p>',
  },
  {
    label: '启动服务', title: '启动 Smart Mic 和 Codex', button: '安装并启动', endpoint: 'install',
    copy: '<p>安装完成后，Mac 每次登录都会启动 Smart Mic 音频桥和 Codex 共享接口。</p><p>如果 Codex 已在运行，安装后请回到 Codex 按 <kbd>⌘Q</kbd> 完全退出，等它自动重新打开。</p>',
  },
  {
    label: '首次发送', title: '发一条语音试试', button: '检测连接', endpoint: 'verify',
    copy: '<p>保持设备开机，等待屏幕显示就绪。如果 Codex 还未自动重开，先按 <kbd>⌘Q</kbd> 完全退出 Codex。</p><ol><li>在目标本地聊天按 <kbd>⌘⌥L</kbd>。</li><li>按设备第三键开始听写，说一句「测试语音发送」，再按一次发送。</li><li>设备显示 <strong>SENT</strong>，且文字出现在聊天里，即可确认完成。</li></ol>',
  },
];
let step = 0;
let deviceState = 'new';
let busy = false;
let verified = false;

function render() {
  const page = steps[step];
  const labels = steps.map((item, index) => {
    const row = document.createElement('li');
    if (index === step) row.setAttribute('aria-current', 'step');
    const number = document.createElement('span');
    number.textContent = index < step ? '✓' : index + 1;
    row.append(number, item.label);
    return row;
  });
  document.querySelector('#steps').replaceChildren(...labels);
  title.textContent = page.title;
  instructions.innerHTML = page.copy;
  fields.replaceChildren();
  status.replaceChildren();
  status.className = '';
  action.textContent = page.button;
  if (step === 2 && deviceState === 'new') {
    fields.innerHTML = '<label class="block">Wi-Fi 名称<input name="ssid" required maxlength="32" autocomplete="off"></label><label class="block">Wi-Fi 密码<input name="password" type="password" maxlength="63" autocomplete="off"></label><label class="confirm"><input name="flash" type="checkbox">首次使用，确认板型为 AI Passport ESP32-C3 8 MB，并同意覆盖应用固件</label><p>设备已经刷好 Smart Mic 固件时，可直接配对。</p>';
  } else if (step === 2) {
    instructions.textContent = 'Mac 的局域网地址发生了变化。保持 USB 连接，再更新设备的连接地址。';
    action.textContent = '更新连接地址';
  }
  title.focus();
}

async function request(endpoint, data = {}) {
  const response = await fetch('/' + endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Smart-Mic-Session': token},
    body: JSON.stringify(data),
  });
  if (!response.ok) throw new Error('向导连接已中断。请重新打开部署程序。');
  const result = await response.json();
  if (!result.ok) throw new Error(result.error);
  return result.result;
}

function finish() {
  title.textContent = '已完成部署';
  instructions.textContent = '下次登录 Mac 后，打开目标聊天即可继续使用 Smart Mic。';
  fields.replaceChildren();
  status.replaceChildren();
  action.hidden = true;
  close.textContent = '关闭向导';
  title.focus();
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  if (verified) {
    finish();
    return;
  }
  const page = steps[step];
  const formData = new FormData(form);
  const data = step === 2 && deviceState === 'new' ?
    {ssid: formData.get('ssid'), password: formData.get('password'), flash: formData.has('flash')} : {};
  status.textContent = step === 2 ? '正在配置设备。请保持 USB 连接。' : '正在处理，请稍候…';
  status.className = '';
  action.disabled = close.disabled = busy = true;
  try {
    const result = await request(page.endpoint, data);
    if (step === 4) {
      status.textContent = 'Codex 共享接口已连接，音频桥已检测到设备连接。完成试发后请勾选下方确认。';
      fields.innerHTML = '<label class="confirm"><input type="checkbox" required>设备显示 SENT，文字已出现在目标聊天</label>';
      action.textContent = '完成部署';
      verified = true;
      fields.querySelector('input').focus();
    } else {
      if (step === 1) deviceState = result;
      step += step === 1 && deviceState === 'paired' ? 2 : 1;
      render();
    }
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
    action.textContent = '重试 · ' + page.button;
  } finally {
    action.disabled = close.disabled = busy = false;
  }
});

close.addEventListener('click', async () => {
  try {
    await request('close');
    document.querySelector('main').textContent = '向导已退出，可以关闭这个页面。';
    close.hidden = true;
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
  }
});
render();
