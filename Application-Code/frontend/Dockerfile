FROM node:20-alpine

WORKDIR /usr/src/app

# * copy package.json and package-lock.json to the container
COPY package*.json ./    

# install dependencies
RUN npm install

# copy the rest of the application code to the container
COPY . .

# start the front-end app
CMD ["npm","start"]

