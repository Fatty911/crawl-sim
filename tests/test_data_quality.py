"""Data-quality regression tests: unit conversion, restricted detection, quality flags."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import crawl_broadnet, crawl_mobile, crawl_unicom
from scripts.merge_data import classify, is_broadband, is_restricted, quality_flags


class TestUnicomUnitConversion:
    def test_mb_to_gb(self):
        assert crawl_unicom._to_gb(1024, "MB") == 1.0
        assert crawl_unicom._to_gb(4096, "MB") == 4.0
        assert crawl_unicom._to_gb(512, "MB") == 0.5

    def test_gb_passthrough(self):
        assert crawl_unicom._to_gb(30, "GB") == 30.0
        assert crawl_unicom._to_gb(20, "") == 20.0

    def test_normalize_honors_data_unit(self):
        raw = {
            "name": "欢居生活会员流量包20元",
            "reportNo": "25JT000001",
            "detailsList": [{
                "name": "欢居生活会员流量包20元",
                "codeType": "套餐",
                "feesStandard": "20",
                "commonData": "1024",
                "dataUnit": "MB",
                "orientTraffic": "0",
                "orientTrafficUnit": "GB",
                "minute": "0",
                "sms": "0",
                "validPeriod": "至2029年12月31日",
                "saleChnl": "线下及线上渠道",
                "useScope": "全网用户",
                "extraFees": "",
                "serviceContent": "欢居生活会员权益+1GB通用流量服务",
            }],
        }
        row = crawl_unicom.normalize(raw, "全国", "加装包/流量包")
        assert row["general_traffic_gb"] == 1.0, f"1024MB must be 1GB, got {row['general_traffic_gb']}"
        assert row["monthly_fee"] == 20.0


class TestBroadnetYearlyFee:
    def test_year_fee_becomes_monthly_average(self):
        raw = {
            "productName": "一年300M宽带5G套餐",
            "productPrice": 88000,
            "productPriceUnit": "年",
            "parentTypeCode": "GZ_TC_KD",
            "domesticTraffic": 0,
            "orientTraffic": 0,
            "domesticCall": 0,
            "sms": 0,
            "otherContent": "",
            "tariffAttr": "",
            "validPeriod": "",
            "saleChannel": "",
            "applicablePeople": "",
            "onlineDay": "",
            "offlineDay": "",
            "filingNumber": "26BJ000001",
        }
        row = crawl_broadnet.normalize(raw, "北京")
        assert row["monthly_fee"] == 73.33, f"88000分/年 should be 73.33元/月, got {row['monthly_fee']}"


class TestMobilePlanType:
    def test_name_based_plan_type(self):
        assert crawl_mobile.parse_card("放心用流量包-10元档\n资费标准:\n10元/月\n方案编号:\n25JT100001\n资费类型:\n套餐\n适用地区:\n北京\n")["plan_type"] == "流量包"
        assert crawl_mobile.parse_card("59元元气卡\n资费标准:\n59元/月\n方案编号:\n25JT100002\n资费类型:\n套餐\n适用地区:\n北京\n国内通用流量\n50GB\n")["plan_type"] == "套餐"


class TestTelecomTrafficUnit:
    """crawl_telecom 服务内容表流量单位解析（2026-08-12 审计实锤：1T 被填成 1.0GB）。"""

    _CARD = (
        "无线宽带(FWA)-市区版-1年（续费不含终端）\n"
        "资费标准：\n73.3元/月\n"
        "服务内容\n通用流量\t定向流量\t语音\n"
        "{values}\n"
    )

    def test_tb_traffic_converted_to_gb(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="1TB\t0MB\t0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 1024.0, out.get("general_traffic_gb")

    def test_t_unit_traffic(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="2T\t0MB\t0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 2048.0

    def test_tb_orient_column(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="10GB\t1TB\t0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 10
        assert out["orient_traffic_gb"] == 1024.0

    def test_gb_untouched(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="50GB\t0MB\t0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 50

    def test_decimal_tb(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="1.5T\t0MB\t0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 1536.0

    def test_space_separated_values(self):
        import scripts.crawl_telecom as ct
        out = ct.parse_card(self._CARD.format(values="20GB 2TB 0分钟"))
        assert out is not None
        assert out["general_traffic_gb"] == 20
        assert out["orient_traffic_gb"] == 2048.0


class TestRestricted:
    def test_growth_plan_restricted(self):
        row = {"plan_name": "畅越冰激凌5G/5G-A套餐成长计划D（北京）-次月生效",
               "use_scope": "畅越冰激凌5G-A套餐109元档及以上档位用户"}
        assert is_restricted(row) is True

    def test_region_note_not_restricted(self):
        row = {"plan_name": "惠民月卡2.0", "use_scope": "全网用户（黑龙江、西藏、江苏、天津、广东除外）"}
        assert is_restricted(row) is False
        row2 = {"plan_name": "升卿卡", "use_scope": "全网用户（仅限广东地区）"}
        assert is_restricted(row2) is False

    def test_exclusive_pack_restricted(self):
        row = {"plan_name": "（元气专属）5元20GB流量加量包", "use_scope": "限元气盒子/MAX版元气盒子专属套餐用户订购"}
        assert is_restricted(row) is True

    def test_campus_restricted(self):
        row = {"plan_name": "2026校园续约月付版-25元档", "use_scope": "校园用户"}
        assert is_restricted(row) is True


class TestQualityFlags:
    def test_traffic_unit_suspect(self):
        row = {"plan_name": "流量包", "monthly_fee": 20, "general_traffic_gb": 1024, "service_content": "会员权益"}
        assert "traffic_unit_suspect" in quality_flags(row)

    def test_large_traffic_explained_ok(self):
        row = {"plan_name": "智家全光臻宽带1719元档套餐", "monthly_fee": 59,
               "general_traffic_gb": 1000, "service_content": "含全国流量1000GB"}
        assert "traffic_unit_suspect" not in quality_flags(row)

    def test_fee_outlier(self):
        row = {"plan_name": "融合套餐", "monthly_fee": 1719, "general_traffic_gb": 1000,
               "service_content": "含全国流量1000GB"}
        assert "fee_outlier" in quality_flags(row)


class TestBroadband:
    def base(self, **kw):
        row = {
            "source": "中国联通", "plan_name": "测试", "report_no": "T1",
            "region": "全国", "plan_type": "套餐",
            "monthly_fee": 59, "general_traffic_gb": 30,
            "orient_traffic_gb": 0, "voice_minutes": 100, "sms": 0,
            "contract": False, "service_content": "", "contract_desc": "",
            "use_scope": "全网用户", "broadband": "",
        }
        row.update(kw)
        return row

    def test_unicom_broadband_field(self):
        raw = {
            "name": "联通智家全光臻宽带全家享1719元档套餐",
            "reportNo": "25JT000002",
            "detailsList": [{
                "name": "联通智家全光臻宽带全家享1719元档套餐",
                "codeType": "套餐",
                "feesStandard": "1719",
                "commonData": "1000",
                "dataUnit": "GB",
                "orientTraffic": "0",
                "orientTrafficUnit": "GB",
                "minute": "8000",
                "sms": "0",
                "broadBand": "1000M",
                "validPeriod": "",
                "saleChnl": "",
                "useScope": "全网用户",
                "extraFees": "",
                "serviceContent": "含全国流量1000GB、全国语音8000分钟",
            }],
        }
        row = crawl_unicom.normalize(raw, "全国", "套餐/融合")
        assert row["broadband"] == "1000M"
        assert is_broadband(row) is True

    def test_mobile_broadband_from_name(self):
        row = {"plan_name": "智家宽带融合套餐", "broadband": "", "service_content": ""}
        assert is_broadband(row) is True

    def test_mobile_plan_not_broadband(self):
        row = {"plan_name": "59元元气卡", "broadband": "", "service_content": "国内上网前3GB按5元/GB收取"}
        assert is_broadband(row) is False

    def test_standalone_broadband_classified(self):
        out = classify(self.base(plan_name="单宽包年预存包-500M（一年）720", broadband="500M",
                                 general_traffic_gb=0))
        assert out["is_broadband"] is True
        # standalone broadband without mobile data must NOT default-show as a phone plan
        assert out["default_show"] is False


class TestBroadbandJudgement:
    """service_content 里的"宽带"字样不能误判为宽带产品（功能描述/否定语境）。"""

    def base(self, **kw):
        row = {"plan_name": "测试", "broadband": "", "use_scope": "", "service_content": ""}
        row.update(kw)
        return row

    def test_marketing_negation_not_broadband(self):
        row = self.base(plan_name="动感地带萌卡-10元",
                        service_content="2、福建：不可同时办理所有营销案(含宽带群组保底业务)3、安徽智慧家庭礼包添加本产品不支持流量语音资源共享")
        assert is_broadband(row) is False

    def test_security_function_not_broadband(self):
        row = self.base(plan_name="5G联通安全管家基础版",
                        service_content="产品内包含2GB全国流量、5G基础速率服务和联通安全管家(包含通话安全、短信安全、亲情守护-手机上网安全、亲情守护-宽带上网安全功能)")
        assert is_broadband(row) is False

    def test_exclusive_listing_not_broadband(self):
        row = self.base(plan_name="四足机器人尊享版",
                        service_content="本活动与硬件类、宽带、权益类等部分合约方案互斥")
        assert is_broadband(row) is False

    def test_real_broadband_in_content(self):
        row = self.base(plan_name="家庭套餐", service_content="含一条100M宽带。")
        assert is_broadband(row) is True

    def test_speedup_in_content(self):
        row = self.base(plan_name="影音包", service_content="合约到期2000M宽带提速自动恢复原价")
        assert is_broadband(row) is True

    def test_ftth_in_content(self):
        row = self.base(plan_name="双网影音包",
                        service_content="1）HFC网络：下行200Mbps/上行11Mbps；FTTH网络：下行200Mbps/上行40Mbps")
        assert is_broadband(row) is True


class TestCampusExclusion:
    """校园专属宽带（限制高校区域）标记 excluded_campus，不进公开 Pages。"""

    def base(self, **kw):
        row = {
            "source": "中国联通", "plan_name": "测试", "report_no": "T1",
            "region": "全国", "plan_type": "套餐",
            "monthly_fee": 59, "general_traffic_gb": 30,
            "orient_traffic_gb": 0, "voice_minutes": 100, "sms": 0,
            "contract": False, "service_content": "", "contract_desc": "",
            "use_scope": "全网用户", "broadband": "",
        }
        row.update(kw)
        return row

    def test_campus_broadband_marked(self):
        out = classify(self.base(plan_name="联通智家沃派校园专属套餐宽带100M0元/年（北京）", broadband="100M",
                                 use_scope="北京校园沃派用户且宽带校园内使用"))
        assert out["is_broadband"] is True
        assert out["excluded_campus"] is True

    def test_campus_in_scope_only(self):
        out = classify(self.base(plan_name="校园宽带300M一年", broadband="300M",
                                 use_scope="校园用户可办理"))
        assert out["excluded_campus"] is True

    def test_campus_data_pack_not_excluded(self):
        # 校园流量卡等非宽带产品不在本次排除范围
        out = classify(self.base(plan_name="校园流量包10元", plan_type="流量包", broadband="",
                                 use_scope="校园用户", general_traffic_gb=20))
        assert out["excluded_campus"] is False

    def test_normal_broadband_not_excluded(self):
        out = classify(self.base(plan_name="300M宽带一年期", broadband="300M", use_scope="全网用户"))
        assert out["excluded_campus"] is False


class TestBroadnetYearlyTierPrice:
    """广电"一年…XX元档"年付档位价兜底（API productPrice 对档位产品不可靠）。"""

    def test_tier_price_extracted(self):
        assert crawl_broadnet._yearly_tier_price("一年100M靓号宽带双享包48元档") == 48.0
        assert crawl_broadnet._yearly_tier_price("一年200M双网影音包38元档") == 38.0
        assert crawl_broadnet._yearly_tier_price("一年300M靓号宽带双享包68元档") == 68.0

    def test_no_tier_no_extract(self):
        assert crawl_broadnet._yearly_tier_price("一年300M宽带5G套餐") is None
        assert crawl_broadnet._yearly_tier_price("59元元气卡") is None

    def test_normalize_uses_tier_price(self):
        raw = {
            "productName": "一年100M靓号宽带双享包48元档",
            "productPrice": 10000,  # 源站返回统一基础价 10000 分/年，不可靠
            "productPriceUnit": "年",
            "parentTypeCode": "GZ_TC_KD",
            "domesticTraffic": 0, "orientTraffic": 0, "domesticCall": 0, "sms": 0,
            "otherContent": "", "tariffAttr": "", "validPeriod": "12个月，到期自动失效",
            "saleChannel": "", "applicablePeople": "", "onlineDay": "", "offlineDay": "",
            "filingNumber": "26BJ500009",
        }
        row = crawl_broadnet.normalize(raw, "北京")
        assert row["monthly_fee"] == 4.0, f"48元档/年 -> 4.0 元/月, got {row['monthly_fee']}"


class TestBroadbandFields:
    """宽带带宽大小（broadband_mbps）与接入方式（access_method）提取。"""

    def base(self, **kw):
        row = {
            "source": "中国联通", "plan_name": "测试", "report_no": "T1",
            "region": "全国", "plan_type": "套餐",
            "monthly_fee": 59, "general_traffic_gb": 30,
            "orient_traffic_gb": 0, "voice_minutes": 100, "sms": 0,
            "contract": False, "service_content": "", "contract_desc": "",
            "use_scope": "全网用户", "broadband": "",
        }
        row.update(kw)
        return row

    def test_pure_m(self):
        out = classify(self.base(plan_name="1000M宽带一年期", broadband="1000M"))
        assert out["broadband_mbps"] == 1000

    def test_uplink_in_parens_ignored(self):
        out = classify(self.base(plan_name="千兆宽带", broadband="1000M（上行40M）"))
        assert out["broadband_mbps"] == 1000

    def test_m_in_cjk_context(self):
        # Python \b 不识别中文字符边界，必须用 (?![0-9A-Za-z])
        out = classify(self.base(plan_name="单宽包年预存包", broadband="一条1000M宽带"))
        assert out["broadband_mbps"] == 1000

    def test_speedup_pack_target(self):
        # 2026-08-12 分类审计：提速包不含宽带线路本身 → is_broadband=False（旧期望 300 是误判）
        out = classify(self.base(plan_name="提速包", broadband="200M提速到300M宽带提速包（12个月）"))
        assert out["is_broadband"] is False
        assert out["broadband_mbps"] is None

    def test_speedup_pack_variants_not_broadband(self):
        # 审计 6 条实锤：千兆提速包/提速小合约/上行提速包 均非宽带线路
        for name, bb in [
            ("宽带千兆提速包（非千兆FTTR）", "带宽提速至1000M"),
            ("千兆提速小合约-120元/12个月（2024存量2年+）", "千兆提速"),
            ("千兆宽带上行提速至100M提速包-50元/月", "上行提速至100M"),
            ("200M提速到300M宽带提速包（12个月）", "200M提速到300M"),
        ]:
            out = classify(self.base(plan_name=name, broadband=bb))
            assert out["is_broadband"] is False, name

    def test_public_ip_service_not_broadband(self):
        # 审计：公网IP 是宽带附加服务，非线路本体
        out = classify(self.base(plan_name="固网宽带-开通公网IP服务10元/月（2024）", broadband="固网宽带"))
        assert out["is_broadband"] is False

    def test_mobile_main_card_subproduct_not_broadband(self):
        # 审计：融合套餐的 5G 移网主卡子产品——宽带仅营销词，本身是移网卡
        out = classify(self.base(
            plan_name="联通智家全光臻宽带全家享99元档套餐5G移网主卡（北京）", broadband="全光臻宽带"))
        assert out["is_broadband"] is False

    def test_traffic_bolt_on_not_broadband(self):
        # 审计：臻宽带融合套餐的流量补充月包——流量加装非线路
        out = classify(self.base(
            plan_name="臻宽带融合套餐166档0元20GB流量补充月包（北京）", broadband="臻宽带融合套餐"))
        assert out["is_broadband"] is False

    def test_real_broadband_with_speedup_desc_not_excluded(self):
        # 误伤回归：真宽带含"提速"描述（FTTR 套餐 content 提速至2000M）不能被 BOLT_ON 排除
        out = classify(self.base(plan_name="爱家福袋FTTR-WiFi7（2000M）", broadband="",
                                 service_content="爱家光网WiFi 7一主一从方案部署，提速至2000M。合约到期2000M宽带提速自动恢复原价"))
        assert out["is_broadband"] is True
        assert out["broadband_mbps"] == 2000

    def test_real_bundle_not_excluded_by_bolt_on_keywords(self):
        # 误伤回归：真实融合宽带套餐（名称无"移网主卡/提速包"等子产品尾缀）
        # 即使 broadband 字段含"提速"字样也不被排除（BOLT_ON_RE 只匹配名称）
        out = classify(self.base(
            plan_name="联通智家全光臻宽带全家享99元档套餐（北京）", broadband="全光臻宽带，含提速服务"))
        assert out["is_broadband"] is True

    def test_qianzhao_word(self):
        out = classify(self.base(plan_name="移动看家尊享福袋千兆版", broadband="",
                                 service_content="含千兆宽带提速服务"))
        assert out["broadband_mbps"] == 1000

    def test_gbps_unit(self):
        out = classify(self.base(plan_name="全家享套餐", broadband="下行最高1Gbps"))
        assert out["broadband_mbps"] == 1000

    def test_mobile_speed_not_bandwidth(self):
        # 5G-A 移网峰值速率不算宽带带宽
        out = classify(self.base(plan_name="5G-A399元套餐", broadband="399元/月，含240GB全国流量（网络最高下行3Gbps，最高上行400Mbps）"))
        assert out["broadband_mbps"] is None

    def test_bundle_bandwidth_from_content(self):
        # bb 字段被截断时，从 service_content 的"加装/含一条"语境兜底
        out = classify(self.base(plan_name="5G-A399元套餐", broadband="399元/月，含240GB全国流量",
                                 service_content="除橙分期合约外，加装2000M宽带（含FTTR一主一从）可享AI权益超市120元额度任选。"))
        assert out["broadband_mbps"] == 2000
        assert out["access_method"] == "FTTR"

    def test_5g_not_bandwidth(self):
        out = classify(self.base(plan_name="一年300M宽带5G套餐", broadband="300M"))
        assert out["broadband_mbps"] == 300  # 5G 不放大 1000 倍

    def test_content_hfc_downlink(self):
        out = classify(self.base(plan_name="双网影音包", broadband="",
                                 service_content="HFC网络：下行200Mbps/上行11Mbps；FTTH网络：下行200Mbps/上行40Mbps"))
        assert out["broadband_mbps"] == 200
        assert out["access_method"] == "光纤"

    def test_access_fwa(self):
        # 2026-08-12 分类审计：FWA 通用流量加装包是流量加装，非宽带线路 → 无接入方式
        out = classify(self.base(plan_name="FWA通用流量加装包", broadband="10元/次，包含FWA无线宽带200GB流量"))
        assert out["is_broadband"] is False
        assert out["access_method"] is None
        assert out["broadband_mbps"] is None

    def test_access_fttr_priority(self):
        out = classify(self.base(plan_name="爱家福袋FTTR-WiFi7（2000M）", broadband="",
                                 service_content="爱家光网WiFi 7一主一从方案部署，提速至2000M。合约到期2000M宽带提速自动恢复原价"))
        assert out["access_method"] == "FTTR"

    def test_non_broadband_none(self):
        out = classify(self.base(plan_name="59元元气卡", broadband="",
                                 service_content="国内上网前3GB按5元/GB收取"))
        assert out["broadband_mbps"] is None
        assert out["access_method"] is None


class TestClassify:
    def base(self, **kw):
        row = {
            "source": "中国联通", "plan_name": "测试套餐", "report_no": "T1",
            "region": "全国", "plan_type": "套餐",
            "monthly_fee": 59, "general_traffic_gb": 30,
            "orient_traffic_gb": 0, "voice_minutes": 100, "sms": 0,
            "contract": False, "service_content": "", "contract_desc": "",
            "use_scope": "全网用户",
        }
        row.update(kw)
        return row

    def test_plan_type_normalization(self):
        # 2026-08-12 分类审计 7 条实锤：会员/场景包/功能包/公网IP/提速包 不是流量包
        cases = [
            ("Mini会员", "流量包", "套餐"),
            ("奔马权益会员", "流量包", "套餐"),
            ("5G-A场景包", "流量包", "套餐"),
            ("5G升级功能包", "流量包", "套餐"),
            ("云智手机服务高配版", "流量包", "套餐"),
            ("固网宽带-开通公网IP服务10元/月（2024）", "流量包", "套餐"),
            ("千兆宽带上行提速至100M提速包-50元/月", "流量包", "套餐"),
            ("200G全国通用流量年包", "流量包", "流量包"),  # 真流量包保持
        ]
        for name, src_type, expect in cases:
            out = classify(self.base(plan_name=name, plan_type=src_type))
            assert out["plan_type"] == expect, f"{name}: {out['plan_type']} != {expect}"

    def test_default_show_ok(self):
        out = classify(self.base())
        assert out["default_show"] is True
        assert out["restricted"] is False
        assert out["quality_flags"] == []

    def test_growth_plan_excluded_from_default(self):
        out = classify(self.base(plan_name="畅越冰激凌5G/5G-A套餐成长计划D（北京）",
                                use_scope="畅越冰激凌5G-A套餐109元档及以上档位用户"))
        assert out["restricted"] is True
        assert out["default_show"] is False

    def test_contract_excluded(self):
        out = classify(self.base(plan_name="送手机合约", monthly_fee=129, contract=True))
        assert out["excluded_phone_contract"] is True
        assert out["default_show"] is False

    def test_data_pack_rule(self):
        out = classify(self.base(plan_type="流量包", monthly_fee=10, general_traffic_gb=20))
        assert out["default_show"] is True

    def test_data_pack_bad_value_excluded(self):
        out = classify(self.base(plan_type="流量包", monthly_fee=10, general_traffic_gb=1024,
                                 service_content="会员权益"))
        assert out["default_show"] is False
        assert "traffic_unit_suspect" in out["quality_flags"]

    def test_region_from_name(self):
        out = classify(self.base(plan_name="畅越冰激凌5G套餐129元基础版（北京）"))
        assert out["region"] == "北京"


class TestFeeNormalization:
    """Multi-period total fees must be converted to equivalent monthly rates."""

    def base(self, **kw):
        return {
            "source": "中国联通",
            "atomic_source_names": ["联通"],
            "plan_name": "",
            "monthly_fee": None,
            "fee_text": "",
            "general_traffic_gb": None,
            "orient_traffic_gb": None,
            "voice_minutes": None,
            "broadband": "",
            "plan_type": "套餐",
            "region": "北京",
            "service_content": "",
            "valid_period": "",
            **kw,
        }

    def test_total_price_five_year(self):
        """5340元五年期 → 89.0/月"""
        out = classify(self.base(
            plan_name="沃长宽全家享300M标准版5340元五年期（北京）",
            monthly_fee=5340.0,
            valid_period="五年。到期视套餐是否在售可续约、可退订",
        ))
        assert out["monthly_fee"] == 89.0
        assert out["original_fee"] == 5340.0
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "五年期"

    def test_total_price_slash_months(self):
        """3204元/24个月 → 133.5/月"""
        out = classify(self.base(
            plan_name="联通智家臻宽带500M公众单宽带3204元/24个月（北京）",
            monthly_fee=3204.0,
            valid_period="24个月",
        ))
        assert out["monthly_fee"] == 133.5
        assert out["original_fee"] == 3204.0
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "24个月"

    def test_monthly_discount_not_converted(self):
        """月费由1719优惠至1599 → 1599 (true monthly, no conversion)"""
        out = classify(self.base(
            plan_name="联通智家全光臻宽带全家享10000M套餐三年期预存1000元合约月费由1719元优惠至1599元（北京）",
            monthly_fee=1599.0,
            valid_period="三年",
        ))
        assert out["monthly_fee"] == 1599.0
        assert out["fee_type"] == "monthly"
        assert out["original_fee"] is None

    def test_monthly_return_not_total(self):
        """预存2136元月返89元-24月 → monthly_fee=89, original=2136"""
        out = classify(self.base(
            plan_name="存量219档全家享500M合约-预存2136元月返89元-24月",
            monthly_fee=2136.0,
            valid_period="24个月",
        ))
        assert out["monthly_fee"] == 89.0
        assert out["original_fee"] == 2136.0
        assert out["fee_type"] == "prepaid_monthly_return"

    def test_no_outlier_flag_after_normalization(self):
        """折算后 monthly_fee=89, 不应触发 fee_outlier"""
        out = classify(self.base(
            plan_name="5340元五年期（北京）",
            monthly_fee=5340.0,
            valid_period="五年",
        ))
        assert "fee_outlier" not in out["quality_flags"]

    def test_normal_monthly_unchanged(self):
        """Regular 69元套餐 should be unchanged"""
        out = classify(self.base(
            plan_name="畅越冰激凌5G套餐69元",
            monthly_fee=69.0,
            general_traffic_gb=20,
        ))
        assert out["monthly_fee"] == 69.0
        assert out["fee_type"] == "monthly"
        assert out["original_fee"] is None
        assert out["billing_period"] is None

    def test_valid_period_only_period(self):
        """名称无期数，但 valid_period 有"36个月" → 按该期数折算"""
        out = classify(self.base(
            plan_name="5G全家享189元套餐2484元趸交合约",
            monthly_fee=2484.0,
            valid_period="36个月",
        ))
        assert out["monthly_fee"] == 69.0
        assert out["fee_type"] == "total_period"
        assert out["original_fee"] == 2484.0

    def test_total_no_slash(self):
        """无斜杠形式 3204元24个月 也应识别为总价"""
        out = classify(self.base(
            plan_name="联通智家臻宽带500M公众单宽带3204元24个月（北京）",
            monthly_fee=3204.0,
            valid_period="24个月",
        ))
        assert out["monthly_fee"] == 133.5
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "24个月"

    def test_total_arabic_year(self):
        """阿拉伯数字年期 3200元3年期 也应识别为总价"""
        out = classify(self.base(
            plan_name="联通智家臻宽带300M单宽带3200元3年期（北京）",
            monthly_fee=3200.0,
            valid_period="3年。到期视套餐是否在售可续约、可退订",
        ))
        assert out["monthly_fee"] == round(3200.0 / 36, 1)
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "3年期"

    def test_date_in_valid_period_not_parsed(self):
        """valid_period 含日期（2029年12月31日）不应被提取为周期"""
        out = classify(self.base(
            plan_name="畅越冰激凌5G套餐129元基础版",
            monthly_fee=129.0,
            valid_period="至2029年12月31日",
        ))
        assert out["monthly_fee"] == 129.0
        assert out["fee_type"] == "monthly"
        assert out["billing_period"] is None

    def test_arabic_year_with_date_valid_period(self):
        """总价+阿拉伯年期+有效期含日期：优先年期而非日期中的12月"""
        out = classify(self.base(
            plan_name="联通智家臻宽带300M单宽带3200元3年期（北京）",
            monthly_fee=3200.0,
            valid_period="至2029年12月31日",
        ))
        assert out["monthly_fee"] == round(3200.0 / 36, 1)
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "3年期"

    def test_year_pack(self):
        """流量年包 350 元 → 350/12≈29.2/月"""
        out = classify(self.base(
            plan_name="流量年包",
            monthly_fee=350.0,
            valid_period="365天",
        ))
        assert out["monthly_fee"] == round(350.0 / 12, 1)
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "1年"

    def test_half_year_pack(self):
        """流量半年包 230 元 → 230/6≈38.3/月"""
        out = classify(self.base(
            plan_name="流量半年包",
            monthly_fee=230.0,
            valid_period="180天",
        ))
        assert out["monthly_fee"] == round(230.0 / 6, 1)
        assert out["fee_type"] == "total_period"

    def test_prepaid_two_year_contract(self):
        """预存988元…两年合约（无“期”字）→ 988/24≈41.2/月"""
        out = classify(self.base(
            plan_name="联通沃派校园5G套餐预存988元5G-A包两年合约（北京）",
            monthly_fee=988.0,
            valid_period="两年",
        ))
        assert out["monthly_fee"] == round(988.0 / 24, 1)
        assert out["fee_type"] == "total_period"
        assert out["billing_period"] == "2年"

    def test_monthly_plan_with_two_year_valid_not_converted(self):
        """月费档套餐（239元套餐）名称无年期信号，valid 里的“两年”不得触发折算"""
        out = classify(self.base(
            plan_name="联通臻宽带239元套餐预存得960元电子券24个月合约（北京）",
            monthly_fee=239.0,
            valid_period="两年。到期视套餐是否在售可续约、可退订",
        ))
        assert out["monthly_fee"] == 239.0
        assert out["fee_type"] == "monthly"

    def test_high_fee_excluded(self):
        """月租>200 标记 excluded_high_fee（不进 Pages）"""
        out = classify(self.base(
            plan_name="联通智家全光臻宽带全家享1719元档（标准原价）套餐5G-A移网主卡（北京）",
            monthly_fee=1719.0,
        ))
        assert out["excluded_high_fee"] is True

    def test_fee_200_boundary_kept(self):
        """月租恰为 200 元不排除"""
        out = classify(self.base(
            plan_name="畅越冰激凌5G套餐200元",
            monthly_fee=200.0,
        ))
        assert out["excluded_high_fee"] is False

    def test_converted_fee_not_excluded(self):
        """折算后 89 元（原5340元五年期）不应被排除"""
        out = classify(self.base(
            plan_name="沃长宽全家享300M标准版5340元五年期（北京）",
            monthly_fee=5340.0,
            valid_period="五年",
        ))
        assert out["monthly_fee"] == 89.0
        assert out["excluded_high_fee"] is False

    def test_monthly_plan_with_24mo_valid_not_converted(self):
        """月费档套餐 valid_period 以“24个月”结尾不得折算（is_total 只用 name）"""
        out = classify(self.base(
            plan_name="畅越冰激凌5G套餐219元",
            monthly_fee=219.0,
            valid_period="24个月，到期视套餐是否在售可续约",
        ))
        assert out["monthly_fee"] == 219.0
        assert out["fee_type"] == "monthly"
        assert out["excluded_high_fee"] is True  # 219>200 仍会被过滤，但不折算
