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
        out = classify(self.base(plan_name="提速包", broadband="200M提速到300M宽带提速包（12个月）"))
        assert out["broadband_mbps"] == 300

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
        out = classify(self.base(plan_name="FWA通用流量加装包", broadband="10元/次，包含FWA无线宽带200GB流量"))
        assert out["access_method"] == "无线(FWA)"
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
