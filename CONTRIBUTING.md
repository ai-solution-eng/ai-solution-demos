# How to Contribute

We welcome all contributions to the repository. If you have updates/fixes to existing demos or net new PCAI validated use case demos you would like to submit, please create a PR. 
Once a PR is created, **we will reach out to you to review your changes**. 

For new demos, we prefer that you have it up-and-running in an environment, and ready to show for the review.
Otherwise, we will import it before (if demo setup is long) or during the review (if demo setup is short).

## Contribution requirements

New demos' file and folder structure is up to you, as long as the following is respected:
- **Your new demo must have its own folder.** You should place it either under **vertical_demos** or **archived_demos**, but this is subject to discussion during the review.
- The folder must contain **a README.md file**, explaining what the demo is, how to set it up, and how to use it. 
- **All the files required to set up and run the demo must be available** in the folder, except for:
  - Frameworks helm charts that are available in [our frameworks repo](https://github.com/ai-solution-eng/frameworks): this does not apply to helm charts you created for custom applications, we need those in the demo folder.
    - If you are using a helm chart of a non-custom application, that is missing from the frameworks repository, please open a PR to add this helm chart to that repo.  
  - Datasets large enough to be unfit for a Git repository: if such a dataset is needed for the demo, we may ask you to send it to us during the review. It will be copied into our cloud storage, and the link to it will be added to the README.
  - Demo video recording: demo recording is optional, but the video itself shouldn't be hosted on GitHub, so if you have one, we'll ask to send it to us during the review. It will be copied into our cloud storage, and the link to it will be added to the README.
- You must know **whether you want to be the demo owner or not**. We will add the following ownership chart to the README if missing:

| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner        | John Doe          | john.doe@hpe.com                   |
| PCAI Deployment Owner | Jane Doe          | jane.doe@hpe.com                   |


The following section provides more detailed guidelines to follow for an ideal contribution. Following them is recommended, but not mandatory, we may merge PRs that do not perfectly follow them. 

## Recommended guidelines

Here are the guidelines that contributors should preferably follow to add new demos):

- Each demo has its own folder. The name of the folder must be **max 32 characters long**

- The README.md file should preferably have the same structure as [**README-template.md**](README-template.md).

- The demo's folder **must not contain** any temporary file. Examples of files and folders that must be avoided are:
    - node_modules
    - .ipynb_checkpoints
    - .vite
    - .venv
    - .idea

- The demo's folder should preferably have the structure depicted below.

```
<demo>
├── deploy
│   ├── chart
│   ├── config
│   ├── data
│   └── notebook
├── doc
├── example
└── source
    ├── chart
    ├── code
    └── docker
```

- **deploy** : any deploy file goes here, in particular:
    - **chart** : put here the helm chart and a logo image that will be used during the import of the framework. If there are more charts (i.e. a frontend and a backend), please create a sub folder for each one. Each time the source code is updated, the chart need to be rebuilt and put here
    - **config** : put here all configuration files (yalm, json, etc...) that will be used during deployment
    - **data** : folder for data to be used during the demo (documents, images). Large files (i.e. bigger than 5MB) should be sent to us and will be copied into our cloud storage. A reference will be added to the README.md file
    - **notebook** : all notebooks that are required during the demo go here, together with any referenced file (like python source code, requirements.txt, sample images). Notebooks will need to download all big files remotely from our cloud storage or copied by hand
- **doc** : documentation goes here. As most of the doc will be inside the README.md file, here we will have just the referenced images and a few more files. A big README.md can be split into sections: in this case, each section file must be put here
- **example** : this folder is meant to store any result produced by the demo that can be of any interest to the audience, like generated images, logs etc... These files can be referenced from the README.md file
- **source** : 
    - **chart** : the unzipped helm chart of the demo goes here. If there are more charts, please create one subfolder for each one
    - **code** : all source code file (not referenced by any notebook) goes here together with any yaml or json file that is needed by the code itself and need to be put close to the code (like inside the same container)
    - **docker** : docker files needed to create containers for charts or models to deploy go here
