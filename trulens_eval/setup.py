from setuptools import find_namespace_packages
from setuptools import setup

langchain_version = "0.0.230" # duplicated in trulens_eval.util, don't know how to dedup

setup(
    name="trulens_eval",
    include_package_data=True,
    packages=find_namespace_packages(
        include=["trulens_eval", "trulens_eval.*"]
    ),
    python_requires='>=3.8',
    install_requires=[
        'cohere>=4.4.1',
        'datasets>=2.12.0',
        'python-dotenv>=1.0.0',
        'kaggle>=1.5.13',
        f'langchain>={langchain_version}',  # required for cost tracking even outside of langchain
        'llama_index>=0.6.24',
        'merkle-json>=1.0.0',
        'millify>=0.1.1',
        'openai>=0.27.6',
        'pinecone-client>=2.2.1',
        'pydantic>=1.10.7',
        'requests>=2.30.0',
        'slack-bolt>=1.18.0',
        'slack-sdk>=3.21.3',
        'streamlit>=1.13.0',  # 1.13.0 needed for colab only. https://stackoverflow.com/questions/74500526/streamlit-via-google-colab-through-localtunnel-does-not-work-anymore
        'streamlit-aggrid>=0.3.4.post3',
        'streamlit-extras>=0.2.7',
        # 'tinydb>=4.7.1',
        'transformers>=4.10.0',
        'typing-inspect==0.8.0',  # langchain with python < 3.9 fix
        'typing_extensions==4.5.0',  # langchain with python < 3.9 fix
        'frozendict>=2.3.8',
        'munch>=3.0.0',
        'ipywidgets>=8.0.6',
        'numpy>=1.23.5',
    ],
)
