from leaps_quant_engine.framework import (
    EqualWeightPortfolioConstructionModel,
    PythonPortfolioConstructionModelLoader,
)


def test_python_portfolio_model_loader_loads_create_portfolio_model(tmp_path):
    portfolio_file = tmp_path / "my_portfolio.py"
    portfolio_file.write_text(
        """
from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel

def create_portfolio_model(params):
    return EqualWeightPortfolioConstructionModel(
        max_portfolio_pct=params["max_portfolio_pct"],
        long_only=params.get("long_only", True),
    )
""",
        encoding="utf-8",
    )

    result = PythonPortfolioConstructionModelLoader().load(
        portfolio_file,
        parameters={"max_portfolio_pct": 0.7, "long_only": False},
    )

    assert isinstance(result.model, EqualWeightPortfolioConstructionModel)
    assert result.model.max_portfolio_pct == 0.7
    assert result.model.long_only is False
    assert result.parameters == {"max_portfolio_pct": 0.7, "long_only": False}


def test_python_portfolio_model_loader_loads_module_object_reference():
    result = PythonPortfolioConstructionModelLoader().load(
        "leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel",
        parameters={"max_portfolio_pct": 0.8},
    )

    assert isinstance(result.model, EqualWeightPortfolioConstructionModel)
    assert result.model.max_portfolio_pct == 0.8
