# Threetierapp with Eks deployment

## Overview
This project involves deploying a robust Three-Tier Application on an Amazon EKS (Elastic Kubernetes Service) cluster. The `Kubernetes-Manifests-Files` directory holds the necessary Kubernetes manifests for deploying the frontend, backend, database, and ingress configurations. Understand and customize these files to suit your deployment needs.

## Prerequisites
Before you begin, ensure you have the following:
* An active **AWS Account** with permissions to create IAM roles, EC2 instances, and EKS clusters.
* A basic understanding of Linux commands and Kubernetes.
* A registered domain name (optional, but recommended for configuring Ingress/Load Balancer).

## Project Details

### Tools Explored
* Kubernetes
* AWS EKS
* Docker
* AWS CLI
* eksctl
* Helm
* AWS Load Balancer Controller

### 🚢 High-Level Overview
This project involves deploying a Three-Tier Application on an Amazon EKS cluster. The process spans setting up IAM, provisioning an EC2 instance to serve as a bastion/management host, installing necessary tools (Docker, kubectl, eksctl, Helm), spinning up an EKS cluster, deploying application manifests, and configuring an AWS Load Balancer to route external traffic to the application.

---

## Getting Started

### Step 1: IAM Configuration
- Create a user `eks-admin` with `AdministratorAccess`.
- Generate Security Credentials: Access Key and Secret Access Key.

### Step 2: EC2 Setup
- Launch an Ubuntu instance in your favourite region (eg. region `us-west-2`).
- SSH into the instance from your local machine.

### Step 3: Install AWS CLI v2
```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
sudo apt install unzip
unzip awscliv2.zip
sudo ./aws/install -i /usr/local/aws-cli -b /usr/local/bin --update
aws configure
```

### Step 4: Install Docker
```bash
sudo apt-get update
sudo apt install docker.io
docker ps
sudo chown $USER /var/run/docker.sock
```

### Step 5: Install kubectl
```bash
curl -o kubectl https://amazon-eks.s3.us-west-2.amazonaws.com/1.19.6/2021-01-05/bin/linux/amd64/kubectl
chmod +x ./kubectl
sudo mv ./kubectl /usr/local/bin
kubectl version --short --client
```

### Step 6: Install eksctl
```bash
curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
sudo mv /tmp/eksctl /usr/local/bin
eksctl version
```

### Step 7: Setup EKS Cluster
```bash
eksctl create cluster --name three-tier-cluster --region us-west-2 --node-type t2.medium --nodes-min 2 --nodes-max 2
aws eks update-kubeconfig --region us-west-2 --name three-tier-cluster
kubectl get nodes
```

### Step 8: Run Manifests
```bash
kubectl create namespace workshop
kubectl apply -f .
kubectl delete -f .
```

### Step 9: Install AWS Load Balancer
```bash
curl -O https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.5.4/docs/install/iam_policy.json
aws iam create-policy --policy-name AWSLoadBalancerControllerIAMPolicy --policy-document file://iam_policy.json
eksctl utils associate-iam-oidc-provider --region=us-west-2 --cluster=three-tier-cluster --approve
eksctl create iamserviceaccount --cluster=three-tier-cluster --namespace=kube-system --name=aws-load-balancer-controller --role-name AmazonEKSLoadBalancerControllerRole --attach-policy-arn=arn:aws:iam::626072240565:policy/AWSLoadBalancerControllerIAMPolicy --approve --region=us-west-2
```
*(Note: Replace the AWS Account ID `626072240565` in the `--attach-policy-arn` flag with your actual AWS Account ID).*

### Step 10: Deploy AWS Load Balancer Controller
```bash
sudo snap install helm --classic
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system --set clusterName=my-cluster --set serviceAccount.create=false --set serviceAccount.name=aws-load-balancer-controller
kubectl get deployment -n kube-system aws-load-balancer-controller
kubectl apply -f full_stack_lb.yaml
```

---

## Cleanup

To delete the EKS cluster:
```bash
eksctl delete cluster --name three-tier-cluster --region us-west-2
```

To clean up the rest of the resources and avoid incurring costs:
- Stop or Terminate the EC2 instance created in Step 2.
- Delete the Load Balancer created in Steps 9 and 10.
- Go to the EC2 console, access the security groups section, and delete any security groups created in previous steps.
