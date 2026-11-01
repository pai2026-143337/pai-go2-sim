#!/usr/bin/env python3
"""公開方策（15,000回）の新形式を、土台の方策ノードが読む形式に変換する。

実ファイルの入出力はPyTorchのあるコンテナ内で行う。
"""
from __future__ import annotations

import argparse
import pickle
from collections.abc import Mapping
from pathlib import Path


ACTOR_SHAPES = {
    "mlp.0.weight": (128, 48), "mlp.0.bias": (128,),
    "mlp.2.weight": (128, 128), "mlp.2.bias": (128,),
    "mlp.4.weight": (128, 128), "mlp.4.bias": (128,),
    "mlp.6.weight": (12, 128), "mlp.6.bias": (12,),
    "distribution.std_param": (12,),
}


def reject_normalization(data: Mapping) -> None:
    """観測の正規化の状態・有効フラグを黙って捨てない。"""
    for key, value in data.items():
        if "norm" in str(key).lower():
            empty = value is None or value is False or (isinstance(value, Mapping) and not value)
            if not empty:
                raise ValueError(f"観測の正規化を含むため変換できません: {key}")
        if isinstance(value, Mapping):
            reject_normalization(value)


def convert_checkpoint(checkpoint: Mapping) -> dict:
    """実物のキーと形状だけを受け入れ、値を変更せず新しい辞書へ写す。"""
    if not isinstance(checkpoint, Mapping):
        raise ValueError("チェックポイントは辞書である必要があります")
    reject_normalization(checkpoint)
    actor = checkpoint.get("actor_state_dict")
    if not isinstance(actor, Mapping):
        raise ValueError("actor_state_dict がありません（未対応の形式）")
    if set(actor) != set(ACTOR_SHAPES):
        raise ValueError(f"想定外の方策キー: 不足={set(ACTOR_SHAPES) - set(actor)}, 余分={set(actor) - set(ACTOR_SHAPES)}")
    for key, shape in ACTOR_SHAPES.items():
        if tuple(getattr(actor[key], "shape", ())) != shape:
            raise ValueError(f"{key} の形状が不一致: 期待={shape}, 実際={getattr(actor[key], 'shape', None)}")
    iteration = checkpoint.get("iter")
    if type(iteration) is not int or iteration < 0:
        raise ValueError(f"iter は0以上の整数が必要です: {iteration!r}")
    return {
        "model_state_dict": {
            ("std" if key == "distribution.std_param" else key.replace("mlp.", "actor.", 1)): value
            for key, value in actor.items()
        },
        "iter": iteration,
    }


def matches(expected: dict, actual: object, torch) -> bool:
    """元の重み・反復番号がすべて保存されているか確かめる。"""
    if not isinstance(actual, Mapping) or set(actual) != set(expected):
        return False
    state = actual["model_state_dict"]
    if actual["iter"] != expected["iter"] or not isinstance(state, Mapping):
        return False
    weights = expected["model_state_dict"]
    return set(state) == set(weights) and all(torch.equal(weights[k], state[k]) for k in weights)


def convert_file(source: Path, destination: Path, *, check_only: bool = False) -> None:
    import torch

    if source.resolve() == destination.resolve():
        raise ValueError("元ファイルと出力先は別にしてください")
    expected = convert_checkpoint(torch.load(source, map_location="cpu", weights_only=True))
    if destination.is_file():
        try:
            actual = torch.load(destination, map_location="cpu", weights_only=True)
            valid = matches(expected, actual, torch)
        except (OSError, RuntimeError, ValueError, TypeError, EOFError, pickle.UnpicklingError):
            valid = False
        if valid:
            print(f"変換済み・全重み一致: {destination} (iter {expected['iter']})")
            return
    if check_only:
        raise ValueError(f"変換済みファイルがないか、元の重みと一致しません: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        torch.save(expected, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"変換完了: {destination} (iter {expected['iter']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="取得・照合済みの raw.pt")
    parser.add_argument("destination", type=Path, help="変換先 checkpoint.pt")
    parser.add_argument("--check", action="store_true", help="変換済みファイルの一致だけを確認する")
    args = parser.parse_args()
    convert_file(args.source, args.destination, check_only=args.check)


if __name__ == "__main__":
    main()
