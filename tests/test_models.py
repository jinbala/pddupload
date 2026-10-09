from pdd_listing_automation.models import Platform, SourceProduct, TaskStatus


def test_platform_aliases() -> None:
    assert Platform.parse("拼多多") is Platform.PINDUODUO
    assert Platform.parse("抖音") is Platform.DOUYIN
    assert Platform.parse("pdd") is Platform.PINDUODUO


def test_source_product_identity_prefers_goods_id() -> None:
    product = SourceProduct(
        source_goods_id="10007",
        source_row_key="test-row-1",
        title="测试商品",
        source_platform=Platform.PINDUODUO,
    )
    assert product.identity == "10007"
    assert TaskStatus.DISCOVERED.value == "discovered"


def test_platform_upload_tab_names() -> None:
    assert Platform.PINDUODUO.upload_record_tab_name == "拼多多 拼多多"
    assert Platform.DOUYIN.upload_record_tab_name == "抖音 抖音"
