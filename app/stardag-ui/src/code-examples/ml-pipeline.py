dataset = Dataset(dump=Dump(), params=preprocess_params)
train_dataset = Subset(dataset, filter=train_partition)
test_dataset = Subset(dataset, filter=test_partition)
trained_model = TrainedModel(
    model=LogisticRegression(),
    dataset=train_dataset,
)
predictions = Predictions(
    trained_model=trained_model,
    dataset=test_dataset,
)
metrics = Metrics(predictions=predictions)
print(metrics.model_dump_json(indent=2))
sd.build(metrics)
