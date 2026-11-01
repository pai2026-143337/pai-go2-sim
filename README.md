# Go2 シミュレーションでの学習済み歩行方策の比較と言語指示

Physical AI 講座 2026 応用編1 の最終課題で使ったコード。Gazebo 上の Unitree Go2 を ROS 2 で動かし、歩行制御（CHAMP と学習済み方策3種）を差し替えて比較した。また、LLM が日本語の文を目標速度に変換して Go2 を動かす操作を試した。学習は行わず、公開されている学習済み方策を実行している。

## 必要なもの

- Docker（Mac の Apple シリコンで動作を確認）
- Python 3（言語指示と記録の実行に使う）

## 手順

1. コンテナを起動する（Mac）。ブラウザで `http://127.0.0.1:6080` を開くとコンテナ内の画面が見える。

   ```bash
   bash docker/run_container.sh
   ```

2. 土台の取得とビルド、学習済み方策の取得、rosbridge の導入（コンテナ内）

   ```bash
   docker exec -it -u ubuntu pai-go2 bash
   bash ~/docker/setup_in_container.sh
   bash ~/docker/setup_rl_in_container.sh
   bash ~/docker/setup_rosbridge_in_container.sh
   ```

3. 歩かせる（コンテナ内）

   ```bash
   source /opt/ros/humble/setup.bash && source ~/ws/install/setup.bash
   ros2 launch go2_config gazebo.launch.py controller:=champ
   ros2 launch go2_config gazebo.launch.py controller:=model checkpoint:=$HOME/ws/models/go2_flat6800/checkpoint.pt
   ```

   方策1・方策3は `go2_flat/checkpoint.pt`・`go2_flat15000/checkpoint.pt` を指定する。

4. Mac 側の Python 環境を用意し、目標速度列を与えて記録する（実験1、Mac）

   ```bash
   bash llm/setup_host.sh
   bash analysis/record_runs.sh 10 champ ai300 ai6800 ai15000
   ```

5. 言語指示で動かす（実験2、Mac。`OPENAI_API_KEY` を環境変数に設定しておく）

   ```bash
   bash llm/record_runs.sh SESSION --provider openai --model gpt-6-luna --controller ai6800
   ```

## 使用した外部のもの（このリポジトリには含めていない）

| もの | 出典 |
|---|---|
| 土台（Go2 の機体モデル、CHAMP、方策の実行ノード） | [rahgirrafi/unitree-go2-ros2](https://github.com/rahgirrafi/unitree-go2-ros2)（`ignition` ブランチ） |
| 方策1（300回） | [Isaac Lab](https://github.com/isaac-sim/IsaacLab) の公開モデル |
| 方策2（6,800回） | [Zhefan-Xu/isaac-go2-ros2](https://github.com/Zhefan-Xu/isaac-go2-ros2) |
| 方策3（15,000回） | [OneSleepyGuy/Isaac-Velocity-Flat-Unitree-Go2-v0-No-Env-Noise](https://huggingface.co/OneSleepyGuy/Isaac-Velocity-Flat-Unitree-Go2-v0-No-Env-Noise) |
| LLM から ROS を使う仕組み | [robotmcp/ros-mcp-server](https://github.com/robotmcp/ros-mcp-server) |
| コンテナのイメージ | [tiryoh/ros2-desktop-vnc](https://hub.docker.com/r/tiryoh/ros2-desktop-vnc) |
