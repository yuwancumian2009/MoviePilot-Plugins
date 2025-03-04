## 1. 外部消息转发使用示列
> #### 1. 群辉事件提醒
>> POST:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_json?apikey=api_token`
	GET:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_form?apikey=api_token`
- HTTP主体：`{"title":"群辉事件提醒：","text":"@@TEXT@@"}`
- POST:
- ![](images/1.png)
- ![](images/1.1.png)
- 参数：`title`、`群辉事件提醒：`
- GET:
- ![](images/2.png)
- ![](images/2.1.png)

> #### 2. QD框架自定义消息
>> POST:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_json?apikey=api_token`
	GET:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_form?apikey=api_token&title={log}&text={t}`
- POST Data：`{"title":"{log}：","text":"{t}"}`
- POST:
- ![](images/3.png)
- GET:
- ![](images/3.1.png)

> #### 3. IYUUPlus开发版
>> POST:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_json?apikey=api_token`
	GET:`IYUUPlus暂未提供`
- 请求Body：`{"title":"{{title}}","text":"{{content}}"}`
- POST:
- ![](images/4.png)

> #### 4. Proxmox Virtual Environment
>> POST:`http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_json?apikey=api_token`
- 正文：
```
        {
        "title":"{{ title }}",
        "text":"{{ severity }}\n{{ escape message }}"
        }
```
- POST:
- ![](images/5.png)