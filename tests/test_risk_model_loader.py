from leaps_quant_engine.framework import (
    BasicRiskManagementModel,
    PythonRiskManagementModelLoader,
    RiskManagementModelLoadError,
)


def test_python_risk_model_loader_loads_class_reference_with_parameters():
    result = PythonRiskManagementModelLoader().load(
        "leaps_quant_engine.framework:BasicRiskManagementModel",
        parameters={},
    )

    assert isinstance(result.model, BasicRiskManagementModel)
    assert result.model_name == "BasicRiskManagementModel"


def test_python_risk_model_loader_loads_file_factory(tmp_path):
    module_path = tmp_path / "risk.py"
    module_path.write_text(
        """
from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits


def create_risk_model(params):
    return BasicRiskManagementModel(
        limits=RiskLimits(max_position_pct=float(params["max_position_pct"]))
    )
""".strip(),
        encoding="utf-8",
    )

    result = PythonRiskManagementModelLoader().load(module_path, parameters={"max_position_pct": 0.2})

    assert isinstance(result.model, BasicRiskManagementModel)
    assert result.model.limits.max_position_pct == 0.2


def test_python_risk_model_loader_rejects_invalid_module(tmp_path):
    module_path = tmp_path / "risk.py"
    module_path.write_text("VALUE = 1", encoding="utf-8")

    try:
        PythonRiskManagementModelLoader().load(module_path)
    except RiskManagementModelLoadError as exc:
        assert "create_risk_model" in str(exc)
    else:
        raise AssertionError("Expected RiskManagementModelLoadError.")
