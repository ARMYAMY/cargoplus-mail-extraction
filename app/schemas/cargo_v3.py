from typing import List
from pydantic import BaseModel, Field


class ContainerInfoItem(BaseModel):
    ContainerNo: str = Field(default="", description="箱号")
    SealNo: str = Field(default="", description="封条号")
    ContSize: str = Field(default="", description="箱尺寸: 20, 40, 45等")
    ContType: str = Field(default="", description="箱类型: GP, HQ, RF, FR, OT, NOR等")
    KGS: str = Field(default="", description="单箱重量数值")
    KGSunit: str = Field(default="", description="重量单位: KGS, LBS等")
    PCS: str = Field(default="", description="单箱件数数值")
    Package: str = Field(default="", description="件数包装单位代码: CARTONS, WOODEN CASE等")
    CBM: str = Field(default="", description="单箱体积数值")
    CBMunit: str = Field(default="", description="体积单位: CBM, CFT等")
    HSCode: str = Field(default="", description="海关商品编码")
    GoodsName: str = Field(default="", description="英文货品名称")
    GoodsNameCN: str = Field(default="", description="中文货品名称")


class CargoV3Output(BaseModel):
    # Shipper
    ShipperName: str = Field(default="", description="发货人主体名称（首行）")
    ShipperAddr: str = Field(default="", description="发货人地址及联系信息原文")
    ShipperTel: str = Field(default="", description="发货人电话")
    ShipperEmail: str = Field(default="", description="发货人邮箱")
    ShipperFax: str = Field(default="", description="发货人传真")

    # Consignee
    ConsigneeName: str = Field(default="", description="收货人主体名称（首行）")
    ConsigneeAddr: str = Field(default="", description="收货人地址及联系信息原文")
    ConsigneeTel: str = Field(default="", description="收货人电话")
    ConsigneeEmail: str = Field(default="", description="收货人邮箱")
    ConsigneeFax: str = Field(default="", description="收货人传真")

    # Notify
    NotifyName: str = Field(default="", description="通知人主体名称（首行）")
    NotifyAddr: str = Field(default="", description="通知人地址及联系信息原文")
    NotifyTel: str = Field(default="", description="通知人电话")
    NotifyEmail: str = Field(default="", description="通知人邮箱")
    NotifyFax: str = Field(default="", description="通知人传真")

    # Ports & Locations
    POR: str = Field(default="", description="收货地代码")
    PORName: str = Field(default="", description="收货地名称")
    POL: str = Field(default="", description="起运港代码")
    POLName: str = Field(default="", description="起运港名称")
    POD: str = Field(default="", description="目的港代码")
    PODName: str = Field(default="", description="目的港名称")
    TransPort: str = Field(default="", description="中转港")
    DeliveryCode: str = Field(default="", description="交货地代码")
    DeliveryName: str = Field(default="", description="交货地名称")

    # Voyage & Dates
    ETD: str = Field(default="", description="预计离港时间")
    ETA: str = Field(default="", description="预计到达时间")
    Vessel: str = Field(default="", description="船名")
    Voyage: str = Field(default="", description="航次")
    CutOffDate: str = Field(default="", description="截关时间")
    SICutOff: str = Field(default="", description="截补料时间")

    # Containers
    ContainerInfo: List[ContainerInfoItem] = Field(default_factory=list, description="集装箱明细列表")
    TotalContainerQty: str = Field(default="", description="集装箱总数量或箱型汇总")

    # Cargo Details
    GoodsName: str = Field(default="", description="英文货物品名")
    GoodsNameCN: str = Field(default="", description="中文货物品名")
    Marks: str = Field(default="", description="唛头")
    HSCode: str = Field(default="", description="商品海关编码")
    Packages: str = Field(default="", description="总件数数值")
    PackagesUnit: str = Field(default="", description="总件数包装单位代码")
    GrossWeight: str = Field(default="", description="总毛重数值")
    GrossWeightUnit: str = Field(default="", description="总毛重单位")
    NetWeight: str = Field(default="", description="总净重数值")
    NetWeightUnit: str = Field(default="", description="总净重单位")
    Volume: str = Field(default="", description="总体积数值")
    VolumeUnit: str = Field(default="", description="总体积单位")

    # Trade & Transport Terms
    Incoterms: str = Field(default="", description="贸易条款: FOB, CIF, EXW等")
    Movement: str = Field(default="", description="运输方式: CY-CY, CY-DOOR等")
    PackingMode: str = Field(default="", description="装箱模式: FCL, LCL等")
    GoodsType: str = Field(default="S", description="货物类型代码: S=普货, R=冷冻, D=危险品, O=超标")
    FreightTerm: str = Field(default="", description="运费条款: PREPAID, COLLECT等")
    Carrier: str = Field(default="", description="承运船公司")
    IsTrucking: str = Field(default="", description="是否拖车")
    IsCustomsDeclare: str = Field(default="", description="是否报关")
    ReleaseBLType: str = Field(default="", description="放单方式: 电放/正本等")

    # Documents & References
    BookingNo: str = Field(default="", description="订舱单号/SO号")
    BLNo: str = Field(default="", description="提单号")
    ContractNo: str = Field(default="", description="合同号")
    Remark: str = Field(default="", description="备注说明")
