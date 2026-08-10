"""Data-quality regression tests: unit conversion, restricted detection, quality flags."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import crawl_broadnet, crawl_mobile, crawl_unicom
from scripts.merge_data import classify, is_restricted, quality_flags


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
