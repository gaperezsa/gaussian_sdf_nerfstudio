resolutions=("64" "128" "192" "256" "320" "384" "448")
for i in ${resolutions[@]} # OR ${list[*]}
do
   echo $i
   CUDA_VISIBLE_DEVICES=4 ns-train gaussian-NeRF-bounded --experiment-name resolution-testing-$i --data data/blender/lego --pipeline.model.f-init ones --pipeline.model.f-transition-function relu --pipeline.model.f-grid-resolution $i --pipeline.model.sigma 0.2 --pipeline.model.g-transition-function identity --pipeline.model.near-plane 2.0 --pipeline.model.far-plane 6.0 --pipeline.model.background-color white --vis wandb blender-data
done