def form(site_options) -> list:
    """
    拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
    """
    return [
        {
            'component': 'VForm',
            'content': [
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'outlined',
                        'class': 'mt-0'
                    },
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {
                                'class': 'd-flex align-center'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'style': 'color: #1976D2;',
                                        'class': 'mr-2'
                                    },
                                    'text': 'mdi-cog'
                                },
                                {
                                    'component': 'span',
                                    'text': '基本设置'
                                }
                            ]
                        },
                        {
                            'component': 'VDivider'
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'enabled',
                                                        'label': '启用插件',
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'notify',
                                                        'label': '发送通知',
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'onlyonce',
                                                        'label': '立即运行一次',
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'outlined',
                        'class': 'mt-3'
                    },
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {
                                'class': 'd-flex align-center'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'style': 'color: #1976D2;',
                                        'class': 'mr-2'
                                    },
                                    'text': 'mdi-clock-outline'
                                },
                                {
                                    'component': 'span',
                                    'text': '执行设置'
                                }
                            ]
                        },
                        {
                            'component': 'VDivider'
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCronField',
                                                    'props': {
                                                        'model': 'cron',
                                                        'label': '执行周期',
                                                        'placeholder': '5位cron表达式，留空自动'
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSelect',
                                                    'props': {
                                                        'model': 'interval_cnt',
                                                        'label': '消息发送间隔(秒)',
                                                        'items': [
                                                            {'title': '1秒', 'value': 1},
                                                            {'title': '2秒', 'value': 2}
                                                        ]
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSelect',
                                                    'props': {
                                                        'model': 'feedback_timeout',
                                                        'label': '反馈等待时间(秒)',
                                                        'items': [
                                                            {'title': '1秒', 'value': 1},
                                                            {'title': '2秒', 'value': 2},
                                                            {'title': '3秒', 'value': 3},
                                                            {'title': '4秒', 'value': 4},
                                                            {'title': '5秒', 'value': 5}
                                                        ]
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'outlined',
                        'class': 'mt-3'
                    },
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {
                                'class': 'd-flex align-center'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'style': 'color: #1976D2;',
                                        'class': 'mr-2'
                                    },
                                    'text': 'mdi-web'
                                },
                                {
                                    'component': 'span',
                                    'text': '站点设置'
                                }
                            ]
                        },
                        {
                            'component': 'VDivider'
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'use_proxy',
                                                        'label': '启用代理',
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'get_feedback',
                                                        'label': '获取反馈',
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                },
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSelect',
                                                    'props': {
                                                        'chips': True,
                                                        'multiple': True,
                                                        'model': 'chat_sites',
                                                        'label': '选择站点',
                                                        'items': site_options
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                },
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12
                                            },
                                            'content': [
                                                {
                                                    'component': 'VTextarea',
                                                    'props': {
                                                        'model': 'sites_messages',
                                                        'label': '自定义消息',
                                                        'rows': 6,
                                                        'placeholder': '每一行一个配置，配置方式：\n'
                                                                        '站点名称|消息内容1|消息内容2|消息内容3|...\n'
                                                                        '同名站点消息配置多行支持消息合并。\n'
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'outlined',
                        'class': 'mt-3'
                    },
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {
                                'class': 'd-flex align-center'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'style': 'color: #1976D2;',
                                        'class': 'mr-2'
                                    },
                                    'text': 'mdi-star-face'
                                },
                                {
                                    'component': 'span',
                                    'text': '任务系统'
                                }
                            ]
                        },
                        {
                            'component': 'VDivider'
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {
                                                'cols': 12,
                                                'md': 4
                                            },
                                            'content': [
                                                {
                                                    'component': 'VSwitch',
                                                    'props': {
                                                        'model': 'medal_bonus',
                                                        'label': '织梦勋章套装奖励',
                                                        'hint': '该功能正在开发中,进度50%',
                                                        'persistent-hint': True,
                                                        'disabled': True
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'outlined',
                        'class': 'mt-3'
                    },
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {
                                'class': 'd-flex align-center'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'style': 'color: #1976D2;',
                                        'class': 'mr-2'
                                    },
                                    'text': 'mdi-information'
                                },
                                {
                                    'component': 'span',
                                    'text': '使用说明'
                                }
                            ]
                        },
                        {
                            'component': 'VDivider'
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'class': 'mt-1',
                                        'text': '执行周期支持：'
                                                '1、5位cron表达式；'
                                                '2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；'
                                                '3、周期不填默认9-23点随机执行1次。'
                                    }
                                },
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'class': 'mt-2',
                                        'text': '获取反馈功能说明：'
                                                '1、获取喊话后的站点反馈(奖励信息)，有助于了解站点对喊话的响应情况；'
                                                '2、反馈信息包括奖励类型、数量和时间，有助于分析站点奖励机制。'
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    ], {
        "enabled": False,
        "notify": False,
        "cron": "",
        "onlyonce": False,
        "interval_cnt": 2,
        "chat_sites": [],
        "sites_messages": "",
        "get_feedback": False,
        "feedback_timeout": 5,
        "use_proxy": True,
        "medal_bonus": False
    }