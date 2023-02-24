alphas=("4.0" "8.0" "12.0" "16.0" "20.0" "24.0")
alpha_increments=("0.0" "0.0005" "0.001")
for j in ${alpha_increments[@]}
do
   for i in ${alphas[@]} # OR ${list[*]}
   do
      CUDA_VISIBLE_DEVICES=7 ns-train gaussian-NeRF-bounded --experiment-name alpha-increments-testing-$i-$j --data data/blender/lego --pipeline.model.f-init ones --pipeline.model.f-transition-function sigmoid --pipeline.model.f-grid-resolution 256 --pipeline.model.sigma 1.0 --pipeline.model.g-transition-function sigmoid --pipeline.model.g-transition-alpha $i --pipeline.model.g-transition-alpha-increments $j --pipeline.model.near-plane 2.0 --pipeline.model.far-plane 6.0 --pipeline.model.background-color white --vis wandb blender-data
   done
done