sigma=("0.2" "0.4" "0.6" "0.8" "1.0" "1.2")
f_function=("relu" "sigmoid")
for j in ${f_function[@]}
do
   for i in ${sigma[@]} # OR ${list[*]}
   do
      CUDA_VISIBLE_DEVICES=6 ns-train gaussian-NeRF-bounded --experiment-name sigma-f_function-testing-$i-$j --data data/blender/lego --pipeline.model.f-init ones --pipeline.model.f-transition-function $j --pipeline.model.f-grid-resolution 256 --pipeline.model.sigma $i --pipeline.model.g-transition-function identity --pipeline.model.near-plane 2.0 --pipeline.model.far-plane 6.0 --pipeline.model.background-color white --vis wandb blender-data
   done
done